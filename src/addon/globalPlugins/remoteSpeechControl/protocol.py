"""Wire protocol and authentication for Remote Speech Control.

Security goals
--------------
1. The shared password never leaves either machine. We HMAC a request payload
   with a key derived from the password; only the MAC is transmitted.
2. Replay is blocked by (a) a +/- 30 s timestamp window and (b) per-session
   nonce tracking on the controlled side.
3. A wrong password produces no observable difference on the wire from the
   right one — verification uses constant-time compare and the controlled
   side responds identically (silently) to bad MACs.
4. Repeated bad MACs trigger a per-session rate limit that locks out further
   auth attempts for a cooldown.
5. PBKDF2-HMAC-SHA256 with 100 000 iterations derives the HMAC key, so an
   attacker who somehow obtains an HMAC cannot trivially brute-force a weak
   password offline.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Dict, List

PROTOCOL_VERSION = 1

CUSTOM_TYPE_PREFIX = "remoteSpeechControl_"

MSG_CAPABILITY = "remoteSpeechControl_capability"
MSG_MUTE_REQUEST = "remoteSpeechControl_mute_request"
MSG_UNMUTE_REQUEST = "remoteSpeechControl_unmute_request"
MSG_STATE = "remoteSpeechControl_state"
MSG_CONSENT_PENDING = "remoteSpeechControl_consent_pending"

ACTION_MUTE = "mute"
ACTION_UNMUTE = "unmute"

NONCE_BYTES = 16
TIMESTAMP_WINDOW_S = 30.0
NONCE_REUSE_WINDOW_S = 90.0
MAX_AUTH_FAILURES = 3
AUTH_LOCKOUT_S = 60.0

_PBKDF2_SALT = b"remoteSpeechControl-v1-salt-fixed"
_PBKDF2_ITERS = 100_000


def _derive_key(password: str) -> bytes:
    if not password:
        return b""
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), _PBKDF2_SALT, _PBKDF2_ITERS
    )


def _payload(action: str, nonce_hex: str, ts: int) -> bytes:
    return "|".join(["remoteSpeechControl", str(PROTOCOL_VERSION), action, nonce_hex, str(ts)]).encode("utf-8")


def fresh_nonce_hex() -> str:
    return os.urandom(NONCE_BYTES).hex()


def make_request(password: str, action: str) -> Dict[str, str]:
    """Build a signed request dict ready to put on the transport."""
    nonce = fresh_nonce_hex()
    ts = int(time.time())
    key = _derive_key(password)
    mac = hmac.new(key, _payload(action, nonce, ts), hashlib.sha256).hexdigest() if key else ""
    return {
        "proto": str(PROTOCOL_VERSION),
        "action": action,
        "nonce": nonce,
        "ts": str(ts),
        "mac": mac,
    }


def verify_request(
    password: str,
    request: Dict[str, str],
    nonce_tracker: "NonceTracker",
    *,
    now: float | None = None,
) -> bool:
    if not password:
        return False
    try:
        proto = int(request.get("proto", "0"))
        action = request.get("action", "")
        nonce = request.get("nonce", "")
        ts = int(request.get("ts", "0"))
        mac_hex = request.get("mac", "")
    except (TypeError, ValueError):
        return False
    if proto != PROTOCOL_VERSION:
        return False
    if action not in (ACTION_MUTE, ACTION_UNMUTE):
        return False
    if not nonce or not mac_hex:
        return False
    real_now = time.time() if now is None else now
    if abs(real_now - ts) > TIMESTAMP_WINDOW_S:
        return False
    if not nonce_tracker.check_and_remember(nonce):
        return False
    key = _derive_key(password)
    expected = hmac.new(key, _payload(action, nonce, ts), hashlib.sha256).hexdigest()
    try:
        provided = bytes.fromhex(mac_hex)
        expected_bytes = bytes.fromhex(expected)
    except ValueError:
        return False
    return hmac.compare_digest(provided, expected_bytes)


class NonceTracker:
    """Reject reused nonces within a sliding window. Reset on session change."""

    def __init__(self, window_seconds: float = NONCE_REUSE_WINDOW_S):
        self._seen: Dict[str, float] = {}
        self._window = window_seconds

    def check_and_remember(self, nonce: str) -> bool:
        now = time.monotonic()
        for n, ts in list(self._seen.items()):
            if now - ts > self._window:
                del self._seen[n]
        if nonce in self._seen:
            return False
        self._seen[nonce] = now
        return True

    def reset(self) -> None:
        self._seen.clear()


class FailureLimiter:
    def __init__(self, max_failures: int = MAX_AUTH_FAILURES, lockout_seconds: float = AUTH_LOCKOUT_S):
        self._failures: List[float] = []
        self._max = max_failures
        self._lockout = lockout_seconds

    def is_locked(self) -> bool:
        now = time.monotonic()
        self._failures = [t for t in self._failures if now - t < self._lockout]
        return len(self._failures) >= self._max

    def record_failure(self) -> None:
        self._failures.append(time.monotonic())

    def reset(self) -> None:
        self._failures.clear()
