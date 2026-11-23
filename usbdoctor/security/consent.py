"""Explicit-consent tokens for destructive or modifying operations.

The UI flow is: engine proposes an :class:`OperationPlan` → user reads the
listed effects and risks → consent is granted → the service obtains a
:class:`ConsentToken` bound to that plan's device signature → the repair
executor validates the token before its first write.  Tokens expire and are
single-use, so a stale approval can never authorise a later, different action.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from usbdoctor.core.errors import UnsafeOperationError
from usbdoctor.core.model import DeviceSignature

DEFAULT_TTL_SECONDS = 900


@dataclass
class ConsentToken:
    token: str
    operation_id: str
    device_path: str
    signature: DeviceSignature
    risks: list[str]
    granted_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + DEFAULT_TTL_SECONDS)
    used: bool = False

    def expired(self) -> bool:
        return time.time() >= self.expires_at

    def describe(self) -> dict:
        return {
            "operation_id": self.operation_id,
            "device_path": self.device_path,
            "risks": list(self.risks),
            "expires_in_seconds": max(0, int(self.expires_at - time.time())),
        }


class ConsentBroker:
    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[str, ConsentToken] = {}
        self._ttl = ttl_seconds

    def grant(
        self,
        *,
        operation_id: str,
        device_path: str,
        signature: DeviceSignature,
        risks: list[str],
    ) -> ConsentToken:
        token = ConsentToken(
            token=secrets.token_urlsafe(24),
            operation_id=operation_id,
            device_path=device_path,
            signature=signature,
            risks=list(risks),
            expires_at=time.time() + self._ttl,
        )
        with self._lock:
            self._tokens[token.token] = token
            self._prune()
        return token

    def consume(self, token_value: str, *, device_path: str, signature: DeviceSignature) -> ConsentToken:
        with self._lock:
            token = self._tokens.get(token_value)
            self._prune()
        if token is None:
            raise UnsafeOperationError(
                "No valid consent token for this operation.",
                hint="Re-run the repair from the UI so a fresh approval can be recorded.",
            )
        if token.used:
            raise UnsafeOperationError("This consent token was already used (single use).")
        if token.expired():
            raise UnsafeOperationError(
                "Consent expired before the operation started.",
                hint="Approve the operation again to continue.",
            )
        if token.device_path != device_path:
            raise UnsafeOperationError(
                "Consent was granted for a different device.",
                device=token.device_path,
                requested=device_path,
            )
        if not token.signature.matches(signature):
            raise UnsafeOperationError(
                "The device changed identity after consent was granted.",
                hint="The drive was replaced or re-plugged. Scan it again before repairing.",
            )
        token.used = True
        return token

    def pending(self) -> list[dict]:
        with self._lock:
            self._prune()
            return [t.describe() for t in self._tokens.values() if not t.used]

    def _prune(self) -> None:
        now = time.time()
        for key in [k for k, t in self._tokens.items() if t.expired() or t.used]:
            # keep used tokens briefly for auditing
            token = self._tokens[key]
            if token.used and now - token.granted_at < 3600:
                continue
            del self._tokens[key]
