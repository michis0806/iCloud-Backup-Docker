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
