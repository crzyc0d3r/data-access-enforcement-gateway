"""Identity: who is asking, and which groups do they belong to?

In a real platform this comes from the platform's own login (SSO, OAuth,
session cookies). Here it is a small in-memory directory plus a signed,
expiring token so the HTTP gateway can prove a request's identity without
trusting anything the client app says about itself.

Demo users:

* ``nina`` - member of ``hr``            -> sees every row.
* ``tom``  - member of ``eng-managers``  -> sees only ``team = 'Engineering'``.
* ``ava``  - member of ``marketing``     -> no policy grants access -> denied.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class User:
    username: str
    display_name: str
    groups: tuple[str, ...] = field(default_factory=tuple)


# Passwords are demo-only. Never do this in production: use your IdP.
_DIRECTORY: dict[str, tuple[str, User]] = {
    "nina": ("nina-demo", User("nina", "Nina (HR)", ("hr",))),
    "tom": ("tom-demo", User("tom", "Tom (Engineering Manager)", ("eng-managers",))),
    "ava": ("ava-demo", User("ava", "Ava (Marketing)", ("marketing",))),
    "admin": ("admin-demo", User("admin", "Platform Admin", ("admin",))),
}


class Directory:
    """Looks up users and signs/verifies bearer tokens."""

    def __init__(self, secret: bytes | None = None, ttl_seconds: int = 3600):
        self._secret = secret or os.environ.get("GATEWAY_SIGNING_KEY", "").encode() or secrets.token_bytes(32)
        self._ttl = ttl_seconds

    # -- lookups -----------------------------------------------------------
    def get(self, username: str) -> User | None:
        entry = _DIRECTORY.get(username)
        return entry[1] if entry else None

    def authenticate(self, username: str, password: str) -> User | None:
        entry = _DIRECTORY.get(username)
        if not entry:
            return None
        expected, user = entry
        if hmac.compare_digest(expected.encode(), password.encode()):
            return user
        return None

    # -- tokens ------------------------------------------------------------
    def issue_token(self, user: User) -> str:
        payload = {"sub": user.username, "exp": int(time.time()) + self._ttl}
        body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")
        sig = hmac.new(self._secret, body, hashlib.sha256).digest()
        return (body + b"." + base64.urlsafe_b64encode(sig).rstrip(b"=")).decode()

    def verify_token(self, token: str) -> User | None:
        try:
            body_b64, sig_b64 = token.encode().split(b".", 1)
            body = base64.urlsafe_b64decode(body_b64 + b"=" * (-len(body_b64) % 4))
            sig = base64.urlsafe_b64decode(sig_b64 + b"=" * (-len(sig_b64) % 4))
        except (ValueError, TypeError):
            return None
        expected = hmac.new(self._secret, body_b64, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(body)
        if payload.get("exp", 0) < time.time():
            return None
        return self.get(payload.get("sub", ""))
