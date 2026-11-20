"""End-to-end demo: three apps, two policies, one place that decides who sees what.

Starts the gateway on a free port, then runs every app as

* Nina (HR)                  -> 15 rows in every app
* Tom  (Engineering Manager) -> 5 rows in every app, narrowed server-side
* Ava  (Marketing)           -> blocked in every app (no policy covers her)

and finishes by printing the audit log summary. No app code changes between
users; the gateway rewrote (or refused) the identical query.

    python demo.py
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

from apps.client import GatewayClient, GatewayError
from apps.comp_table_cli import render_table, run as run_cli
from apps.comp_table_export import CompensationExporter
from apps.comp_table_web import render_page
from enforcement.db import seeded_connection
from enforcement.gateway import GatewayState, start_background
from enforcement.policies import PolicySet
from enforcement.resource import EnforcedResource

ROOT = Path(__file__).resolve().parent
USERS = [("nina", "nina-demo", "HR"), ("tom", "tom-demo", "Engineering Manager"), ("ava", "ava-demo", "Marketing")]


def banner(text: str) -> None:
    print("\n" + "=" * 72 + f"\n{text}\n" + "=" * 72)


def main() -> int:
    resource = EnforcedResource(seeded_connection(), PolicySet.load(ROOT / "policies.json"))
    server, url = start_background(GatewayState(resource))
    print(f"gateway for {resource.name!r} listening at {url}")
    print("policies: " + "; ".join(f"{p.name} -> groups {list(p.groups)}" for p in resource.policy_set.policies))

    ok = True
    try:
        for user, pw, label in USERS:
            banner(f"{user} ({label})")
            # App 1: terminal table
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rows_cli = run_cli(user, pw, url)
                print(f"[comp_table_cli]    {len(rows_cli)} rows")
                print("    " + render_table(rows_cli).replace("\n", "\n    "))
            except GatewayError as exc:
                rows_cli = None
                print(f"[comp_table_cli]    BLOCKED -> {exc}")
            # App 2: web page
            try:
                rows_web = GatewayClient(url).login(user, pw).query()["rows"]
                page = render_page(user, rows_web)
                print(f"[comp_table_web]    {len(rows_web)} rows embedded in a {len(page):,}-byte HTML page")
            except GatewayError as exc:
                rows_web = None
                print(f"[comp_table_web]    BLOCKED -> {exc}")
            # App 3: CSV export
            try:
                csv_text = CompensationExporter(user, pw, url).export("csv")
                rows_csv = max(csv_text.count("\n") - 1, 0)
                print(f"[comp_table_export] {rows_csv} data rows in CSV")
            except GatewayError as exc:
                rows_csv = None
                print(f"[comp_table_export] BLOCKED -> {exc}")

            counts = {len(rows_cli) if rows_cli is not None else None,
                      len(rows_web) if rows_web is not None else None, rows_csv}
            agree = len(counts) == 1
            ok &= agree
            print(f"-> all three apps agree: {agree}")

        banner("audit log (what the platform recorded)")
        for e in resource.audit.entries():
            tail = f"{e.rows_returned} rows" if e.rows_returned is not None else f"reason: {e.reason}"
            print(f"{e.user:5s} {e.outcome:9s} {tail}")
        print("summary:", resource.audit.summary())
    finally:
        server.shutdown()
        server.server_close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
