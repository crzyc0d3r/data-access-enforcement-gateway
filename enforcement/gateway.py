"""HTTP gateway: the front door apps talk to instead of the database.

Endpoints (JSON in, JSON out):

* ``POST /auth/login``                 ``{"username","password"}`` -> ``{"token", ...}``
* ``GET  /me``                         who the bearer token belongs to
* ``POST /resources/<name>/query``     ``{"sql": "...", "params": [...]}`` -> rows
* ``GET  /audit``                      the audit log (``hr`` / ``admin`` groups only)
* ``GET  /``                           a short HTML landing page

Identity comes from the bearer token issued at login, never from anything in
the query payload, so an app cannot claim to be a different user.

Run it directly::

    python -m enforcement.gateway --port 8780
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .identity import Directory
from .resource import AccessDenied, EnforcedResource

ROOT = Path(__file__).resolve().parent.parent
_QUERY_PATH = re.compile(r"^/resources/([\w-]+)/query$")

INDEX_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Enforced data gateway</title>
<style>body{font-family:system-ui,sans-serif;max-width:46rem;margin:3rem auto;line-height:1.5;padding:0 1rem}
code{background:#f3f3f3;padding:.1rem .3rem;border-radius:3px}</style></head><body>
<h1>Enforced data gateway</h1>
<p>Resource <code>{resource}</code> is <strong>{state}</strong>. Every query from every app passes through
the same access policies; apps never hold database credentials.</p>
<ol><li><code>POST /auth/login</code> with <code>{{"username":"tom","password":"tom-demo"}}</code></li>
<li><code>POST /resources/{resource}/query</code> with <code>{{"sql":"SELECT * FROM employees"}}</code>
and <code>Authorization: Bearer &lt;token&gt;</code></li>
<li><code>GET /audit</code> as <code>nina</code> to see what was narrowed or blocked.</li></ol>
</body></html>"""


class GatewayState:
    def __init__(self, resource: EnforcedResource, directory: Directory | None = None):
        self.resource = resource
        self.directory = directory or Directory()


def make_handler(state: GatewayState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "EnforcedGateway/1.0"

        # -- helpers -------------------------------------------------------
        def _json(self, status: int, payload) -> None:
            body = json.dumps(payload, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, status: int, html: str) -> None:
            body = html.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return {}

        def _current_user(self):
            auth = self.headers.get("Authorization", "")
            if not auth.lower().startswith("bearer "):
                return None
            return state.directory.verify_token(auth[7:].strip())

        def log_message(self, fmt, *args):  # keep test output quiet
            if getattr(self.server, "verbose", False):
                super().log_message(fmt, *args)

        # -- routes ----------------------------------------------------------
        def do_GET(self):
            if self.path == "/":
                html = INDEX_HTML.replace("{resource}", state.resource.name).replace(
                    "{state}", "enforced" if state.resource.policy_set.enforced else "NOT enforced")
                return self._html(HTTPStatus.OK, html)
            user = self._current_user()
            if user is None:
                return self._json(HTTPStatus.UNAUTHORIZED, {"error": "login required"})
            if self.path == "/me":
                grants = {t: asdict(g) for t, g in state.resource.effective_grants(user).items()}
                return self._json(HTTPStatus.OK, {"user": user.username, "display_name": user.display_name,
                                                  "groups": list(user.groups), "grants": grants})
            if self.path == "/audit":
                if not set(user.groups) & {"hr", "admin"}:
                    return self._json(HTTPStatus.FORBIDDEN, {"error": "audit log is restricted to hr/admin"})
                return self._json(HTTPStatus.OK, {"entries": [asdict(e) for e in state.resource.audit.entries()],
                                                  "summary": state.resource.audit.summary()})
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self):
            if self.path == "/auth/login":
                body = self._read_json()
                user = state.directory.authenticate(str(body.get("username", "")), str(body.get("password", "")))
                if user is None:
                    return self._json(HTTPStatus.UNAUTHORIZED, {"error": "invalid credentials"})
                return self._json(HTTPStatus.OK, {"token": state.directory.issue_token(user),
                                                  "user": user.username, "groups": list(user.groups)})

            match = _QUERY_PATH.match(self.path)
            if not match:
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            if match.group(1) != state.resource.name:
                return self._json(HTTPStatus.NOT_FOUND, {"error": f"unknown resource {match.group(1)!r}"})
            user = self._current_user()
            if user is None:
                return self._json(HTTPStatus.UNAUTHORIZED, {"error": "login required"})
            body = self._read_json()
            sql = body.get("sql")
            if not isinstance(sql, str) or not sql.strip():
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "body must contain a non-empty 'sql' string"})
            params = body.get("params") or []
            try:
                result = state.resource.query(user, sql, params)
            except AccessDenied as exc:
                return self._json(HTTPStatus.FORBIDDEN, {"error": str(exc), "blocked": True})
            return self._json(HTTPStatus.OK, {"columns": result.columns, "rows": result.rows,
                                              "row_count": len(result.rows), "narrowed": result.narrowed,
                                              "policies_applied": result.policies_applied})

    return Handler


def serve(state: GatewayState, host: str = "127.0.0.1", port: int = 8780, verbose: bool = False) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(state))
    server.verbose = verbose  # type: ignore[attr-defined]
    return server


def start_background(state: GatewayState, host: str = "127.0.0.1", port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    """Start the gateway in a daemon thread; returns (server, base_url). Port 0 = pick a free port."""
    server = serve(state, host, port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://{host}:{server.server_address[1]}"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the enforced data gateway.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--db", default=":memory:", help="SQLite path (default: in-memory, seeded on start)")
    parser.add_argument("--policies", default=str(ROOT / "policies.json"))
    parser.add_argument("--audit-log", default=None, help="optional JSONL file to append audit entries to")
    args = parser.parse_args(argv)

    resource = EnforcedResource.from_files(args.db, args.policies, args.audit_log)
    server = serve(GatewayState(resource), args.host, args.port, verbose=True)
    print(f"Enforced gateway for {resource.name!r} on http://{args.host}:{args.port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
