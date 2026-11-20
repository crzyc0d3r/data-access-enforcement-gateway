"""Shared gateway client used by the three apps (stdlib only).

The client never sees a database credential; it only knows a gateway URL and
the user's login. The same unfiltered query is used by every app::

    SELECT id, name, team, role, salary, last_raise FROM employees ORDER BY name
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_GATEWAY = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8780")
RESOURCE = os.environ.get("GATEWAY_RESOURCE", "compensation_db")
COMPENSATION_QUERY = "SELECT id, name, team, role, salary, last_raise FROM employees ORDER BY name"


class GatewayError(RuntimeError):
    def __init__(self, status: int, message: str, blocked: bool = False):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.blocked = blocked


class GatewayClient:
    def __init__(self, base_url: str = DEFAULT_GATEWAY, resource: str = RESOURCE):
        self.base_url = base_url.rstrip("/")
        self.resource = resource
        self.token: str | None = None
        self.user: str | None = None

    def _call(self, method: str, path: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read() or b"{}")
            except json.JSONDecodeError:
                body = {}
            raise GatewayError(exc.code, body.get("error", exc.reason), bool(body.get("blocked"))) from None

    def login(self, username: str, password: str) -> "GatewayClient":
        out = self._call("POST", "/auth/login", {"username": username, "password": password})
        self.token, self.user = out["token"], out["user"]
        return self

    def query(self, sql: str = COMPENSATION_QUERY, params: list | None = None) -> dict:
        return self._call("POST", f"/resources/{self.resource}/query", {"sql": sql, "params": params or []})

    def me(self) -> dict:
        return self._call("GET", "/me")

    def audit(self) -> dict:
        return self._call("GET", "/audit")


def fetch_compensation(username: str, password: str, gateway: str = DEFAULT_GATEWAY) -> dict:
    """Convenience: log in and run the shared compensation query."""
    return GatewayClient(gateway).login(username, password).query()
