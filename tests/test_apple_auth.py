"""Offline regressions for explicit Apple challenges and partial login success."""

from unittest.mock import Mock

import pytest
from pyicloud import PyiCloudService
from pyicloud.exceptions import PyiCloudAPIResponseException

from app.services.apple_auth import InteractiveICloudService, SessionCompletionRequired


@pytest.fixture
def api(tmp_path):
    client = InteractiveICloudService(
        "test@example.com", "unused", cookie_directory=str(tmp_path), authenticate=False,
    )
    client._auth_data = {
        "mode": "sms",
        "trustedPhoneNumbers": [
            {"id": 7, "obfuscatedNumber": "+49 *** 77", "pushMode": "voice"},
            {"id": 9, "obfuscatedNumber": "+49 *** 99"},
        ],
    }
    client._session = Mock()
    client.session.headers = {"User-Agent": "test"}
    client.session.data = {}
    response = Mock()
    response.raise_for_status.return_value = None
    client.session.get.return_value = response
    client.session.put.return_value = response
    client.session.post.return_value = response
    client.data = {"hsaTrustedBrowser": False, "dsInfo": {"hsaVersion": 2}}
    client._validate_token = Mock(return_value=dict(client.data))

    def trust():
        client._requires_mfa = False
        client.data = {
            "hsaTrustedBrowser": True, "dsInfo": {"hsaVersion": 2},
            "webservices": {"drivews": {"url": "https://example.com/drive"}},
        }
        return True

    client.trust_session = Mock(side_effect=trust)
    return client



def hsa2_response(payload, status=409):
    import json
    from requests import Response

    response = Response()
    response.status_code = status
    response.headers["Content-Type"] = "application/json"
    response._content = json.dumps(payload).encode()
    return response


@pytest.mark.parametrize("method", ["push", "sms"])
def test_real_session_pipeline_accepts_explicitly_valid_hsa2_conflict(api, monkeypatch, method, tmp_path):
    # Exercise pyicloud's actual response normalization, not just a mocked
    # exception. The transport is offline; only requests.Session.request is fake.
    from pyicloud.session import PyiCloudSession
    from requests import Session

    api.request_challenge(method, "7")
    api._session = PyiCloudSession(api, "offline-test", str(tmp_path))
    response = hsa2_response({
        "authenticationType": "hsa2", "securityCode": {"valid": True},
    })
    transport = Mock(return_value=response)
    monkeypatch.setattr(Session, "request", transport)
    assert api.validate_selected_code("123456")
    assert api.code_verified
    api.trust_session.assert_called_once()
    transport.assert_called_once()
    expected = "/phone/securitycode" if method == "sms" else "/trusteddevice/securitycode"
    assert transport.call_args.kwargs["url"].endswith(expected)


@pytest.mark.parametrize("payload", [
    {"authenticationType": "hsa2"},
    {"authenticationType": "hsa2", "securityCode": {"valid": False}},
    {"authenticationType": "hsa2", "securityCode": {"valid": "true"}},
    {"authenticationType": "hsa2", "securityCode": {"valid": True, "securityCodeLocked": True}},
    {"authenticationType": "hsa2", "securityCode": {"valid": True}, "serviceErrors": [{"code": "-21669"}]},
    {"authenticationType": "hsa2", "securityCode": {"valid": True}, "errorCode": "-21669"},
    {"authenticationType": "hsa2", "securityCode": []},
    [],
])
def test_hsa2_conflict_is_not_automatically_success(api, payload):
    from pyicloud.exceptions import PyiCloud2FARequiredException

    api.request_challenge("sms", "7")
    api.session.post.side_effect = PyiCloud2FARequiredException(
        "test@example.com", hsa2_response(payload),
    )
    with pytest.raises(PyiCloud2FARequiredException):
        api.validate_selected_code("123456")
    assert not api.code_verified
    api.trust_session.assert_not_called()


def test_accepted_conflict_still_requires_trusted_session_and_does_not_reuse_code(api):
    from pyicloud.exceptions import PyiCloud2FARequiredException

    api.request_challenge("sms", "7")
    api.session.post.side_effect = PyiCloud2FARequiredException(
        "test@example.com", hsa2_response({
            "authenticationType": "hsa2", "securityCode": {"valid": True},
        }),
    )
    trust = api.trust_session.side_effect
    api.trust_session.side_effect = None
    api.trust_session.return_value = False
    with pytest.raises(SessionCompletionRequired):
        api.validate_selected_code("123456")
    assert api.code_verified
    api.trust_session.side_effect = trust
    assert api.validate_selected_code("123456")
    api.session.post.assert_called_once()


def test_malformed_conflict_is_not_accepted(api):
    from pyicloud.exceptions import PyiCloud2FARequiredException

    response = hsa2_response({})
    response._content = b"not json"
    assert not api._code_accepted_in_conflict(PyiCloud2FARequiredException("test@example.com", response))


def test_non_conflict_status_is_not_accepted(api):
    from pyicloud.exceptions import PyiCloud2FARequiredException

    response = hsa2_response({
        "authenticationType": "hsa2", "securityCode": {"valid": True},
    }, status=403)
    assert not api._code_accepted_in_conflict(PyiCloud2FARequiredException("test@example.com", response))


def test_constructor_pauses_apple_and_automatic_library_delivery(tmp_path, monkeypatch):
    seen = []

    def authenticate(client, **kwargs):
        seen.append(kwargs)
        # This is the automatic hook pyicloud invokes after SRP.
        assert client.request_2fa_code() is False

    monkeypatch.setattr(PyiCloudService, "authenticate", authenticate)
    InteractiveICloudService("test@example.com", "unused", cookie_directory=str(tmp_path))
    assert seen == [{"pause_2fa": True}]


def test_nested_phone_metadata_and_id_zero(api):
    api._auth_data = {"phoneNumberVerification": {
        "trustedPhoneNumber": {"id": 0},
        "trustedPhoneNumbers": [{"id": 0}, {"id": 3}],
    }}
    assert [p["id"] for p in api.trusted_phones()] == [0, 3]
    api.session.put.assert_not_called()
    api.session.get.assert_not_called()


def test_push_never_requests_sms_and_verifies_device_despite_sms_metadata(api):
    assert api.request_challenge("push")
    assert api.request_challenge("push")  # double click
    api.session.get.assert_called_once()
    api.session.put.assert_not_called()
    assert api.validate_selected_code("123456")
    api.session.post.assert_called_once()
    assert api.session.post.call_args.args[0].endswith("/trusteddevice/securitycode")
    api.trust_session.assert_called_once()
    assert api.get_webservice_url("drivews") == "https://example.com/drive"


def test_sms_uses_selected_number_and_sms_not_voice(api):
    assert api.request_challenge("sms", "7")
    assert api.request_challenge("sms", "7")
    api.session.put.assert_called_once()
    assert api.session.put.call_args.kwargs["json"] == {"phoneNumber": {"id": 7}, "mode": "sms"}
    api.session.get.assert_not_called()
    assert api.validate_selected_code("123456")
    assert api.session.post.call_args.args[0].endswith("/phone/securitycode")
    assert api.session.post.call_args.kwargs["json"]["phoneNumber"] == {"id": 7}
    assert api.session.post.call_args.kwargs["json"]["mode"] == "sms"


def test_unknown_phone_does_not_send_code(api):
    with pytest.raises(ValueError):
        api.request_challenge("sms", "100")
    api.session.put.assert_not_called()
    api.session.get.assert_not_called()


def test_push_failure_has_no_automatic_retry_or_sms_fallback(api):
    api.session.get.side_effect = TimeoutError("timeout")
    with pytest.raises(TimeoutError):
        api.request_challenge("push")
    api.session.get.assert_called_once()
    api.session.put.assert_not_called()
    assert api.challenge_method is None


def test_bridge_push_failure_does_not_fall_back_to_sms(api):
    api._supports_trusted_device_bridge = Mock(return_value=True)
    api._trusted_device_bridge = Mock()
    api._trusted_device_bridge.start.side_effect = RuntimeError("bridge unavailable")
    with pytest.raises(RuntimeError):
        api.request_challenge("push")
    api.session.put.assert_not_called()
    api.session.get.assert_not_called()


def test_bridge_code_is_verified_in_the_original_challenge(api):
    bridge_state = Mock(uses_legacy_trusted_device_verifier=False)
    api._supports_trusted_device_bridge = Mock(return_value=True)
    api._trusted_device_bridge = Mock()
    api._trusted_device_bridge.start.return_value = bridge_state
    api._trusted_device_bridge.validate_code.return_value = False
    api.request_challenge("push")
    assert api.validate_selected_code("123456") is False
    assert api._trusted_device_bridge_state is bridge_state
    api._trusted_device_bridge.validate_code.return_value = True
    assert api.validate_selected_code("654321")
    api.session.post.assert_not_called()


def test_invalid_code_is_not_success_and_challenge_can_be_retried(api):
    api.request_challenge("sms", "9")
    api.session.post.side_effect = PyiCloudAPIResponseException("Wrong code", -21669)
    assert api.validate_selected_code("123456") is False
    api.trust_session.assert_not_called()
    assert api.challenge_method == "sms"
    assert not api.code_verified


def test_http_failure_cannot_be_mistaken_for_success(api):
    api.request_challenge("sms", "7")
    api.session.post.return_value.raise_for_status.side_effect = RuntimeError("HTTP 403")
    with pytest.raises(RuntimeError, match="HTTP 403"):
        api.validate_selected_code("123456")
    api.trust_session.assert_not_called()


def test_server_error_after_success_recovers_from_verified_token(api):
    api.request_challenge("push")
    api.session.post.side_effect = PyiCloudAPIResponseException("Already consumed", -21669)
    api._validate_token.return_value = {
        "hsaTrustedBrowser": True, "hsaChallengeRequired": False,
        "webservices": {"drivews": {"url": "https://example.com/drive"}},
    }
    assert api.validate_selected_code("123456")
    assert not api.requires_2fa
    assert api.code_verified
    api.trust_session.assert_not_called()


def test_valid_but_untrusted_token_does_not_mask_wrong_code(api):
    api.request_challenge("push")
    api.session.post.side_effect = PyiCloudAPIResponseException("Wrong code", -21669)
    api._validate_token.return_value = {"dsInfo": {"dsid": "123"}, "hsaTrustedBrowser": False}
    assert api.validate_selected_code("123456") is False


def test_trust_failure_after_accepted_code_recovers_without_new_login(api):
    api.request_challenge("push")
    api.trust_session.side_effect = TimeoutError("completion timeout")
    api._validate_token.return_value = {"hsaTrustedBrowser": True, "hsaChallengeRequired": False}
    assert api.validate_selected_code("123456")
    assert api.session.get.call_count == 1  # only the chosen push
    api.session.put.assert_not_called()


def test_partial_success_retries_completion_without_consuming_code_twice(api):
    api.request_challenge("push")
    api.trust_session.side_effect = None
    api.trust_session.return_value = False
    with pytest.raises(SessionCompletionRequired, match="Code akzeptiert"):
        api.validate_selected_code("123456")
    assert api.code_verified
    api._validate_token.return_value = {"hsaTrustedBrowser": True}
    assert api.validate_selected_code("123456")
    api.session.post.assert_called_once()  # same one-time code is never resubmitted


def test_switching_channel_updates_verifier(api):
    api.request_challenge("sms", "7")
    api.request_challenge("push")
    assert api.validate_selected_code("123456")
    assert api.session.post.call_args.args[0].endswith("/trusteddevice/securitycode")


@pytest.mark.parametrize("code", ["", "12", "abcdef", "１２３４５６"])
def test_invalid_input_never_reaches_apple(api, code):
    api.request_challenge("push")
    with pytest.raises(ValueError):
        api.validate_selected_code(code)
    api.session.post.assert_not_called()


def test_validation_without_channel_is_rejected(api):
    with pytest.raises(ValueError, match="zuerst"):
        api.validate_selected_code("123456")
    api.session.post.assert_not_called()


def test_real_srp_login_suppresses_both_automatic_channels(api, monkeypatch):
    from pyicloud import base
    from pyicloud.exceptions import PyiCloud2FARequiredException

    srp_user = Mock()
    srp_user.start_authentication.return_value = ("test@example.com", b"public")
    srp_user.process_challenge.return_value = b"proof"
    srp_user.H_AMK = b"server-proof"
    monkeypatch.setattr(base.srp, "User", Mock(return_value=srp_user))
    monkeypatch.setattr(base, "SrpPassword", Mock())
    init_response = Mock()
    init_response.json.return_value = {
        "salt": "AA==", "b": "AA==", "c": "challenge", "iteration": 1, "protocol": "s2k",
    }
    api.session.post.side_effect = [
        init_response, PyiCloud2FARequiredException("test@example.com", Mock()),
    ]
    api._get_mfa_auth_options = Mock(return_value=api._auth_data)
    api._srp_authentication(pause_2fa=True)
    assert api.session.post.call_args.kwargs["json"]["pause2FA"] is True
    api.session.put.assert_not_called()
    assert api.session.get.call_count == 1  # authorize/signin only, no device push
    assert api.requires_2fa
    assert api.challenge_method is None


def test_expired_challenge_does_not_consume_code(api):
    api.request_challenge("push")
    api.auth_started_at -= 601
    with pytest.raises(ValueError, match="abgelaufen"):
        api.validate_selected_code("123456")
    api.session.post.assert_not_called()


# ---- pyicloud 2026.10.0 interface guards ----------------------------------

def test_private_pyicloud_interface_used_by_adapter_still_exists(tmp_path):
    """Fail loudly on a pyicloud bump that renames a hook the adapter relies on."""
    import inspect

    from pyicloud.hsa2_bridge import TrustedDeviceBridgeBootstrapper, TrustedDeviceBridgeState

    for name in (
        "_get_mfa_auth_options", "_augment_webauthn_options",
        "_clear_trusted_device_bridge_state", "_get_auth_headers",
        "_set_two_factor_delivery_state", "_supports_trusted_device_bridge",
        "_current_hsa2_boot_context", "_validate_token", "_update_state",
        "_srp_authentication", "trust_session", "request_2fa_code",
        "is_trusted_session", "requires_2fa", "requires_2sa",
    ):
        assert hasattr(PyiCloudService, name), name
    assert "pause_2fa" in inspect.signature(PyiCloudService.__init__).parameters
    assert "pause_2fa" in inspect.signature(PyiCloudService._srp_authentication).parameters

    client = PyiCloudService(
        "test@example.com", "unused", cookie_directory=str(tmp_path), authenticate=False,
    )
    for attr in (
        "_auth_data", "_auth_endpoint", "_requires_mfa", "_pause_2fa",
        "_two_factor_code_requested", "_trusted_device_bridge",
        "_trusted_device_bridge_state", "session",
    ):
        assert hasattr(client, attr), attr
    assert isinstance(client._trusted_device_bridge, TrustedDeviceBridgeBootstrapper)
    assert set(inspect.signature(TrustedDeviceBridgeBootstrapper.start).parameters) >= {
        "session", "auth_endpoint", "headers", "boot_context", "user_agent",
    }
    assert set(inspect.signature(TrustedDeviceBridgeBootstrapper.validate_code).parameters) >= {
        "session", "auth_endpoint", "headers", "bridge_state", "code",
    }
    assert "uses_legacy_trusted_device_verifier" in dir(TrustedDeviceBridgeState)


def test_mfa_options_use_a_single_html_request_without_json_probe(api):
    # pyicloud 2026.10.0 would add a second GET with Accept: application/json
    # for accounts without security keys. The adapter keeps the 2.7.0 shape.
    api._auth_data = {}
    api.session.get.return_value.json.return_value = {"trustedPhoneNumbers": [{"id": 1}]}
    assert [p["id"] for p in api.trusted_phones()] == [1]
    api.session.get.assert_called_once()
    assert api.session.get.call_args.kwargs["headers"]["Accept"] == "text/html"
    api.session.put.assert_not_called()
    api.session.post.assert_not_called()


def test_upstream_mfa_options_would_probe_json_twice(api):
    # Guard: if pyicloud drops the JSON probe, the override above is obsolete.
    api.session.get.return_value.json.return_value = {"trustedPhoneNumbers": [{"id": 1}]}
    PyiCloudService._get_mfa_auth_options(api)
    api.session.get.assert_called_once()  # our override is in effect
    api.session.get.reset_mock()
    PyiCloudService._augment_webauthn_options(api, {})
    api.session.get.assert_called_once()
    assert api.session.get.call_args.kwargs["headers"]["Accept"] == "application/json"


def test_real_srp_login_with_real_mfa_options_sends_no_challenge(api, monkeypatch):
    from pyicloud import base
    from pyicloud.exceptions import PyiCloud2FARequiredException

    srp_user = Mock()
    srp_user.start_authentication.return_value = ("test@example.com", b"public")
    srp_user.process_challenge.return_value = b"proof"
    srp_user.H_AMK = b"server-proof"
    monkeypatch.setattr(base.srp, "User", Mock(return_value=srp_user))
    monkeypatch.setattr(base, "SrpPassword", Mock())
    init_response = Mock()
    init_response.json.return_value = {
        "salt": "AA==", "b": "AA==", "c": "challenge", "iteration": 1, "protocol": "s2k",
    }
    api.session.post.side_effect = [
        init_response, PyiCloud2FARequiredException("test@example.com", Mock()),
    ]
    api.session.get.return_value.json.return_value = {"trustedPhoneNumbers": [{"id": 1}]}
    api._auth_data = {}
    api._srp_authentication(pause_2fa=True)
    urls = [c.args[0] if c.args else c.kwargs.get("url", "") for c in api.session.get.call_args_list]
    # authorize/signin + the HTML auth shell; nothing that delivers a code.
    assert len(urls) == 2
    assert not any("verify" in str(u) for u in urls)
    api.session.put.assert_not_called()
    assert api.requires_2fa
    assert api.challenge_method is None


def _real_session(api, tmp_path):
    from pyicloud.session import PyiCloudSession

    api._session = PyiCloudSession(api, "offline-test", str(tmp_path))


@pytest.mark.parametrize("status", [403, 409])
def test_account_lock_during_validation_surfaces_and_is_not_success(api, monkeypatch, tmp_path, status):
    from pyicloud.exceptions import PyiCloudAccountLockedException, PyiCloudFailedLoginException
    from requests import Session

    api.request_challenge("sms", "7")
    _real_session(api, tmp_path)
    response = hsa2_response({
        "authenticationType": "hsa2", "securityCode": {"valid": True},
        "serviceErrors": [{"code": "-20209"}],
    }, status=status)
    monkeypatch.setattr(Session, "request", Mock(return_value=response))
    with pytest.raises(PyiCloudAccountLockedException) as info:
        api.validate_selected_code("123456")
    # Still a failed login for callers that only know the base class.
    assert isinstance(info.value, PyiCloudFailedLoginException)
    assert not api.code_verified
    api.trust_session.assert_not_called()


def test_unreachable_icloud_during_validation_keeps_challenge(api, monkeypatch, tmp_path):
    import requests
    from pyicloud.exceptions import PyiCloudConnectionException
    from requests import Session

    api.request_challenge("push")
    _real_session(api, tmp_path)
    transport = Mock(side_effect=requests.exceptions.ConnectionError("down"))
    monkeypatch.setattr(Session, "request", transport)
    with pytest.raises(PyiCloudConnectionException):
        api.validate_selected_code("123456")
    assert not api.code_verified
    assert api.challenge_method == "push"
    api.trust_session.assert_not_called()


def test_upstream_409_helper_is_weaker_than_adapter_check(api):
    # Documents why the adapter keeps its own 409 check: pyicloud only looks at
    # securityCode.valid and would accept a locked/cooldown or erroring body.
    from pyicloud.exceptions import PyiCloud2FARequiredException

    for payload in (
        {"securityCode": {"valid": True, "securityCodeLocked": True}, "authenticationType": "hsa2"},
        {"securityCode": {"valid": True}, "serviceErrors": [{"code": "-21669"}], "authenticationType": "hsa2"},
        {"securityCode": {"valid": True}},
    ):
        response = hsa2_response(payload)
        assert PyiCloudService._is_accepted_security_code_conflict(response)
        assert not api._code_accepted_in_conflict(
            PyiCloud2FARequiredException("test@example.com", response)
        )
