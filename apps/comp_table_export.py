"""App 3 - a CSV / JSON exporter for the "Team Compensation" data.

A third shape: a small class-based tool that writes a file for spreadsheets
or downstream scripts. Still no access logic of its own.

    python -m apps.comp_table_export --user tom --password tom-demo --format csv --out comp.csv
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path

from .client import DEFAULT_GATEWAY, GatewayClient, GatewayError


class CompensationExporter:
    def __init__(self, user: str, password: str, gateway: str = DEFAULT_GATEWAY):
        self.client = GatewayClient(gateway).login(user, password)

    def fetch(self) -> list[dict]:
        return self.client.query()["rows"]

    def to_csv(self, rows: list[dict]) -> str:
        buf = io.StringIO()
        if rows:
            writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        return buf.getvalue()

    def to_json(self, rows: list[dict]) -> str:
        return json.dumps(rows, indent=2)

    def export(self, fmt: str = "csv", out: Path | None = None) -> str:
        rows = self.fetch()
        text = self.to_csv(rows) if fmt == "csv" else self.to_json(rows)
        if out:
            out.write_text(text, encoding="utf-8")
        return text


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Export the compensation table.")
    p.add_argument("--user", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--format", choices=["csv", "json"], default="csv")
    p.add_argument("--out", type=Path, default=None, help="write here instead of stdout")
    p.add_argument("--gateway", default=DEFAULT_GATEWAY)
    a = p.parse_args(argv)
    try:
        text = CompensationExporter(a.user, a.password, a.gateway).export(a.format, a.out)
    except GatewayError as exc:
        print(f"Access denied by the data platform: {exc}", file=sys.stderr)
        return 2
    if a.out:
        print(f"wrote {a.out}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
