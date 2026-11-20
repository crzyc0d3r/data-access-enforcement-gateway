"""The enforced resource: the only object that is allowed to touch the database.

Apps hand it (user, sql). It looks up the user's groups, merges the matching
policies, rewrites the SQL, runs it, and writes an audit entry. If the
resource is enforced and no policy covers the user, the query is blocked
before it reaches the database.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .audit import AuditLog
from .identity import User
from .policies import PolicyError, PolicySet, TableGrant, rewrite


class AccessDenied(PermissionError):
    """The effective policy does not allow this query."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[dict]
    policies_applied: list[str]
    executed_sql: str
    narrowed: bool = False
    grants: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.rows)


class EnforcedResource:
    def __init__(
        self,
        conn: sqlite3.Connection,
        policy_set: PolicySet,
        audit: AuditLog | None = None,
    ):
        self.conn = conn
        self.policy_set = policy_set
        self.audit = audit or AuditLog()
        self._tables = self._list_tables()

    @classmethod
    def from_files(cls, db_path: str | Path, policies_path: str | Path, audit_path: str | Path | None = None):
        from .db import seeded_connection
        return cls(seeded_connection(db_path), PolicySet.load(policies_path), AuditLog(audit_path))

    @property
    def name(self) -> str:
        return self.policy_set.resource

    def _list_tables(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------
    def effective_grants(self, user: User) -> dict[str, TableGrant]:
        return self.policy_set.effective_grants(user.groups)

    def query(self, user: User, sql: str, params: tuple | list = ()) -> QueryResult:
        policies = self.policy_set.matching(user.groups)
        names = [p.name for p in policies]
        base = dict(user=user.username, groups=list(user.groups), resource=self.name,
                    policies=names, original_sql=sql)

        if self.policy_set.enforced and not policies:
            reason = "resource is enforced and no policy grants this user access"
            self.audit.record(outcome="blocked", executed_sql=None, rows_returned=None, reason=reason, **base)
            raise AccessDenied(reason)

        grants = self.effective_grants(user) if self.policy_set.enforced else {
            t: TableGrant("full") for t in self._tables
        }
        try:
            executed = rewrite(sql, grants, self._tables)
        except PolicyError as exc:
            self.audit.record(outcome="blocked", executed_sql=None, rows_returned=None, reason=str(exc), **base)
            raise AccessDenied(str(exc)) from exc

        try:
            cur = self.conn.execute(executed, tuple(params))
        except sqlite3.Error as exc:
            self.audit.record(outcome="blocked", executed_sql=executed, rows_returned=None,
                              reason=f"database rejected query: {exc}", **base)
            raise AccessDenied(f"database rejected query: {exc}") from exc

        columns = [d[0] for d in cur.description] if cur.description else []
        rows = [dict(zip(columns, r)) for r in cur.fetchall()]
        if cur.description is None:
            self.conn.commit()
        narrowed = executed.strip() != sql.strip().rstrip(";")
        self.audit.record(outcome="narrowed" if narrowed else "allowed", executed_sql=executed,
                          rows_returned=len(rows), **base)
        return QueryResult(columns, rows, names, executed, narrowed,
                           {t: g.__dict__ for t, g in grants.items()})
