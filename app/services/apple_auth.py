"""Explicit, single-channel 2FA for pyicloud 2.7.0.

Keep this adapter in sync with the other iCloud application. pyicloud's
automatic delivery and implicit SMS fallback are unsuitable for a channel picker.
Private hooks are covered by offline tests; the dependency is pinned accordingly.
"""

import re
import threading
from time import monotonic

from pyicloud import PyiCloudService
from pyicloud.exceptions import PyiCloudAPIResponseException


class SessionCompletionRequired(RuntimeError):
    """Apple accepted the code, but session completion could not be confirmed."""


class InteractiveICloudService(PyiCloudService):
    def __init__(self, *args, **kwargs):
        self._challenge_lock = threading.RLock()
        self.challenge_method = None
        self.challenge_phone = None
        self.code_verified = False
        self.auth_started_at = monotonic()
        # Suppress Apple's automatic challenge during SRP as well as pyicloud's
        # automatic delivery. A paused session is never treated as authenticated.
        kwargs["pause_2fa"] = True
        super().__init__(*args, **kwargs)

    def request_2fa_code(self):
        """Constructor hook: wait for the user's explicit channel selection."""
        return False

    @property
    def pending_auth_expired(self):
        return monotonic() - self.auth_started_at > 600

    def trusted_phones(self):
        if not self._auth_data:
            self._auth_data = self._get_mfa_auth_options()
        sources = [self._auth_data, self._auth_data.get("phoneNumberVerification", {})]
        phones = {}
        for source in sources:
            single = source.get("trustedPhoneNumber")
            candidates = list(source.get("trustedPhoneNumbers") or [])
            if isinstance(single, dict):
                candidates.append(single)
            for phone in candidates:
                if isinstance(phone, dict) and phone.get("id") is not None:
                    phones.setdefault(str(phone["id"]), phone)
        return list(phones.values())

    def request_challenge(self, method, phone_id=None):
        """Send only the selected challenge; repeated clicks reuse it."""
        with self._challenge_lock:
            if self.pending_auth_expired:
                raise ValueError("Anmeldung abgelaufen. Bitte Reauth erneut starten.")
            if method not in ("push", "sms"):
                raise ValueError("Bitte Apple Push oder SMS auswaehlen.")
            if self.code_verified:
                raise SessionCompletionRequired(
                    "Code bereits akzeptiert. Bitte die Anmeldung erneut bestaetigen."
                )
            if self.challenge_method == method and (
                method == "push" or str(self.challenge_phone["id"]) == str(phone_id)
            ):
                return True
            phones = self.trusted_phones()
            self._clear_trusted_device_bridge_state()
            self.challenge_method = None
            self.challenge_phone = None
            headers = self._get_auth_headers({"Accept": "application/json"})
            if method == "sms":
                phone = next((p for p in phones if str(p["id"]) == str(phone_id)), None)
                if phone is None:
                    raise ValueError("Bitte eine verfuegbare Telefonnummer auswaehlen.")
                phone_payload = {"id": phone["id"]}
                if "nonFTEU" in phone:
                    phone_payload["nonFTEU"] = phone["nonFTEU"]
                response = self.session.put(
                    f"{self._auth_endpoint}/verify/phone",
                    json={"phoneNumber": phone_payload, "mode": "sms"},
                    headers=headers,
                )
                response.raise_for_status()
                self.challenge_phone = phone_payload
                self._set_two_factor_delivery_state("sms")
            else:
                # Use Apple's bridge when offered. Unlike request_2fa_code(),
                # deliberately do not fall back to SMS if the push fails.
                if self._supports_trusted_device_bridge():
                    self._trusted_device_bridge_state = self._trusted_device_bridge.start(
                        session=self.session,
                        auth_endpoint=self._auth_endpoint,
                        headers=headers,
                        boot_context=self._current_hsa2_boot_context(),
                        user_agent=self.session.headers["User-Agent"],
                    )
                else:
                    response = self.session.get(
                        f"{self._auth_endpoint}/verify/trusteddevice", headers=headers
                    )
                    response.raise_for_status()
                self._set_two_factor_delivery_state("trusted_device")
            self.challenge_method = method
            self.auth_started_at = monotonic()
            self._two_factor_code_requested = True
            return True

    def _refresh_verified_session(self):
        """Probe existing cookies only; never restart SRP or send another code."""
        try:
            data = self._validate_token()
        except Exception:
            return False
        if not isinstance(data, dict) or not data.get("hsaTrustedBrowser"):
            return False
        if data.get("hsaChallengeRequired", False):
            return False
        self.data = data
        self._requires_mfa = False
        self._auth_data = {}
        self._clear_trusted_device_bridge_state()
        self._update_state()
        return True

    def validate_selected_code(self, code):
        with self._challenge_lock:
            if self.code_verified and self.is_trusted_session and not self.requires_2fa and not self.requires_2sa:
                return True
            if self.pending_auth_expired:
                raise ValueError("Anmeldung abgelaufen. Bitte Reauth erneut starten.")
            if self.challenge_method is None:
                raise ValueError("Bitte zuerst Apple Push oder SMS anfordern.")
            code = code.strip()
            if not re.fullmatch(r"[0-9]{6}", code):
                raise ValueError("Bitte den sechsstelligen Code eingeben.")
            if not self.code_verified:
                try:
                    if self.challenge_method == "sms":
                        response = self.session.post(
                            f"{self._auth_endpoint}/verify/phone/securitycode",
                            json={
                                "phoneNumber": self.challenge_phone,
                                "securityCode": {"code": code},
                                "mode": "sms",
                            },
                            headers=self._get_auth_headers({"Accept": "application/json"}),
                        )
                        response.raise_for_status()
                    else:
                        bridge = self._trusted_device_bridge_state
                        if bridge is not None and not bridge.uses_legacy_trusted_device_verifier:
                            accepted = self._trusted_device_bridge.validate_code(
                                session=self.session,
                                auth_endpoint=self._auth_endpoint,
                                headers=self._get_auth_headers({"Accept": "application/json"}),
                                bridge_state=bridge,
                                code=code,
                            )
                            if not accepted:
                                self.code_verified = self._refresh_verified_session()
                                return self.code_verified
                        else:
                            response = self.session.post(
                                f"{self._auth_endpoint}/verify/trusteddevice/securitycode",
                                json={"securityCode": {"code": code}},
                                headers=self._get_auth_headers({"Accept": "application/json"}),
                            )
                            response.raise_for_status()
                    self.code_verified = True
                except Exception as exc:
                    # Some responses arrive after Apple has already persisted
                    # successful authentication. Confirm that via /validate.
                    if self._refresh_verified_session():
                        self.code_verified = True
                        return True
                    if isinstance(exc, PyiCloudAPIResponseException) and str(
                        getattr(exc, "code", "")
                    ) == "-21669":
                        return False
                    raise

            try:
                self.trust_session()
            except Exception:
                # The trust request may have succeeded despite a later failure.
                pass
            if self.is_trusted_session and not self.requires_2fa and not self.requires_2sa:
                self._update_state()
                return True
            if self._refresh_verified_session():
                return True
            raise SessionCompletionRequired(
                "Code akzeptiert, aber der Abschluss der Anmeldung konnte noch "
                "nicht bestaetigt werden. Bitte erneut bestaetigen; es wird kein "
                "neuer Code gesendet."
            )
