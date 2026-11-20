"""App 2 - a one-page web "Team Compensation" table with a live search box.

Written as a tiny HTTP server that renders HTML and filters client-side in a
few lines of JavaScript. Different layout and code shape from the CLI app,
same query, same absence of any access logic.

    python -m apps.comp_table_web --user nina --password nina-demo --port 8781
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .client import DEFAULT_GATEWAY, GatewayClient, GatewayError

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Team Compensation</title>
<style>
 body{font-family:system-ui,sans-serif;margin:2rem auto;max-width:60rem;padding:0 1rem;color:#222}
 input{width:100%;padding:.6rem;font-size:1rem;margin:.5rem 0 1rem;border:1px solid #bbb;border-radius:6px}
 table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:.5rem .6rem;border-bottom:1px solid #e5e5e5}
 th{background:#fafafa;font-weight:600}td.num{text-align:right;font-variant-numeric:tabular-nums}
 .meta{color:#666;font-size:.9rem}
</style></head><body>
<h1>Team Compensation</h1>
<p class="meta">Signed in as <strong>__USER__</strong> &middot; <span id="count"></span> employees</p>
<input id="q" placeholder="Search name, team or role&hellip;" autofocus>
<table><thead><tr><th>Name</th><th>Team</th><th>Role</th><th>Salary</th><th>Last raise</th></tr></thead>
<tbody id="rows"></tbody></table>
<script>
const data = __DATA__;
const tbody = document.getElementById('rows'), count = document.getElementById('count');
const fmt = n => '$' + Number(n).toLocaleString();
function draw(q){
  q = q.trim().toLowerCase();
  const rows = data.filter(r => !q || Object.values(r).some(v => String(v).toLowerCase().includes(q)));
  tbody.innerHTML = rows.map(r => `<tr><td>${r.name}</td><td>${r.team}</td><td>${r.role}</td>
    <td class="num">${fmt(r.salary)}</td><td>${r.last_raise}</td></tr>`).join('');
  count.textContent = rows.length + (q ? ' of ' + data.length : '');
}
document.getElementById('q').addEventListener('input', e => draw(e.target.value));
draw('');
</script></body></html>"""


def render_page(user: str, rows: list[dict]) -> str:
    safe_rows = [{k: html.escape(str(v)) if isinstance(v, str) else v for k, v in r.items()} for r in rows]
    return PAGE.replace("__USER__", html.escape(user)).replace("__DATA__", json.dumps(safe_rows))


def make_handler(user: str, password: str, gateway: str):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                rows = GatewayClient(gateway).login(user, password).query()["rows"]
                body, status = render_page(user, rows), HTTPStatus.OK
            except GatewayError as exc:
                body = f"<h1>Access denied</h1><p>The data platform refused this query: {html.escape(str(exc))}</p>"
                status = HTTPStatus.FORBIDDEN
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass
    return Handler


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Web compensation table.")
    p.add_argument("--user", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--port", type=int, default=8781)
    p.add_argument("--gateway", default=DEFAULT_GATEWAY)
    a = p.parse_args(argv)
    server = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(a.user, a.password, a.gateway))
    print(f"Team Compensation (web) for {a.user} at http://127.0.0.1:{a.port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
