"""App 1 - terminal "Team Compensation" table with a search box (``--search``).

Written in a plain procedural style. Note what is *not* here: no role check,
no WHERE clause, no idea who should see which rows. It just renders results.

    python -m apps.comp_table_cli --user tom --password tom-demo --search eng
"""

from __future__ import annotations

import argparse
import sys

from .client import DEFAULT_GATEWAY, GatewayClient, GatewayError

COLUMNS = ["name", "team", "role", "salary", "last_raise"]


def render_table(rows: list[dict], columns: list[str] = COLUMNS) -> str:
    if not rows:
        return "(no rows)"
    def fmt(c: str, val) -> str:
        return f"${val:,.0f}" if c == "salary" and isinstance(val, (int, float)) else str(val)

    cells = [{c: fmt(c, r.get(c, "")) for c in columns} for r in rows]
    widths = {c: max(len(c), *(len(row[c]) for row in cells)) for c in columns}
    line = "  ".join(c.upper().ljust(widths[c]) for c in columns)
    sep = "  ".join("-" * widths[c] for c in columns)
    body = ["  ".join(row[c].ljust(widths[c]) for c in columns) for row in cells]
    return "\n".join([line, sep, *body])


def search(rows: list[dict], needle: str) -> list[dict]:
    needle = needle.lower().strip()
    if not needle:
        return rows
    return [r for r in rows if any(needle in str(v).lower() for v in r.values())]


def run(user: str, password: str, gateway: str = DEFAULT_GATEWAY, needle: str = "") -> list[dict]:
    client = GatewayClient(gateway).login(user, password)
    result = client.query()
    rows = search(result["rows"], needle)
    print(f"Team Compensation  (signed in as {user}, {len(result['rows'])} rows returned)")
    print(render_table(rows))
    return rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Terminal compensation table.")
    p.add_argument("--user", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--search", default="", help="filter rows containing this text")
    p.add_argument("--gateway", default=DEFAULT_GATEWAY)
    a = p.parse_args(argv)
    try:
        run(a.user, a.password, a.gateway, a.search)
    except GatewayError as exc:
        print(f"Access denied by the data platform: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
