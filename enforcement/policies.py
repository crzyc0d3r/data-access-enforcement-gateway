"""Access policies and the SQL rewrite that enforces them.

A *policy* grants one or more groups access to one or more tables of a
resource. Per table it says either

* ``"access": "full"``  - supported reads and row edits, no rewrite, or
* ``"access": "read"``  - SELECT only, optionally narrowed by a
  ``row_filter`` (a SQL predicate) and a ``columns`` allow-list.

Enforcement works by **rewriting the table reference**, not by trusting the
app to add a WHERE clause. ``FROM employees`` becomes::

    FROM (SELECT id, name, ... FROM employees WHERE team = 'Engineering') AS employees

so whatever the app wrote on top keeps working, but can only ever see the
rows and columns the policy allows. A query that mentions a column outside
the allow-list fails at the database with "no such column", which is the
desired outcome: the data never leaves.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

class PolicyError(ValueError):
    """Raised when a query is not allowed under the effective policy."""


@dataclass(frozen=True)
class TableGrant:
    access: str                       # "full" | "read"
    row_filter: str | None = None     # SQL predicate, None = all rows
    columns: tuple[str, ...] | None = None  # None = all columns

    def merged_with(self, other: "TableGrant") -> "TableGrant":
        """Union of two grants for the same table (most permissive wins)."""
        if self.access == "full" or other.access == "full":
            return TableGrant("full")
        if self.row_filter is None or other.row_filter is None:
            row_filter = None
        else:
            row_filter = f"({self.row_filter}) OR ({other.row_filter})"
        if self.columns is None or other.columns is None:
            columns = None
        else:
            columns = tuple(dict.fromkeys((*self.columns, *other.columns)))
        return TableGrant("read", row_filter, columns)


@dataclass(frozen=True)
class Policy:
    name: str
    groups: tuple[str, ...]
    tables: dict[str, TableGrant]
    description: str = ""

    def applies_to(self, groups: Iterable[str]) -> bool:
        return bool(set(self.groups) & set(groups))


@dataclass
class PolicySet:
    resource: str
    enforced: bool
    policies: list[Policy] = field(default_factory=list)

    # -- loading -----------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> "PolicySet":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def from_dict(cls, data: dict) -> "PolicySet":
        policies = []
        for p in data.get("policies", []):
            tables = {}
            for table, grant in p.get("tables", {}).items():
                access = grant.get("access", "read")
                if access not in ("full", "read"):
                    raise ValueError(f"policy {p['name']!r}: unknown access {access!r}")
                cols = grant.get("columns")
                tables[table] = TableGrant(
                    access=access,
                    row_filter=grant.get("row_filter"),
                    columns=tuple(cols) if cols else None,
                )
            policies.append(Policy(p["name"], tuple(p.get("groups", [])), tables, p.get("description", "")))
        return cls(data["resource"], bool(data.get("enforced", True)), policies)

    # -- evaluation --------------------------------------------------------
    def matching(self, groups: Iterable[str]) -> list[Policy]:
        groups = list(groups)
        return [p for p in self.policies if p.applies_to(groups)]

    def effective_grants(self, groups: Iterable[str]) -> dict[str, TableGrant]:
        """Merge every matching policy into one grant per table."""
        grants: dict[str, TableGrant] = {}
        for policy in self.matching(groups):
            for table, grant in policy.tables.items():
                grants[table] = grants[table].merged_with(grant) if table in grants else grant
        return grants


# ---------------------------------------------------------------------------
# SQL rewriting
# ---------------------------------------------------------------------------

def _parse_statement(sql: str) -> exp.Expression:
    try:
        statements = [s for s in sqlglot.parse(sql, read="sqlite") if s is not None]
    except SqlglotError as exc:
        raise PolicyError("invalid or unsupported SQL") from exc
    if len(statements) != 1:
        raise PolicyError("exactly one statement is required")
    tree = statements[0]
    # CTE binding and database administration are outside this demo's dialect.
    if tree.find(exp.With) or tree.find(exp.Into):
        raise PolicyError("CTEs and SELECT INTO are not supported")
    if not isinstance(tree, (exp.Select, exp.SetOperation, exp.Update, exp.Insert, exp.Delete)):
        raise PolicyError("statement type not supported")
    return tree


def classify_statement(sql: str) -> str:
    """Classify one supported parsed statement, rejecting unsupported SQL."""
    tree = _parse_statement(sql)
    return "read" if isinstance(tree, (exp.Select, exp.SetOperation)) else "write"


def tables_referenced(sql: str, known_tables: Iterable[str]) -> set[str]:
    known = {t.casefold(): t for t in known_tables}
    return {known[t.name.casefold()] for t in _parse_statement(sql).find_all(exp.Table)
            if t.name.casefold() in known}


def rewrite(sql: str, grants: dict[str, TableGrant], known_tables: Iterable[str]) -> str:
    """Apply ``grants`` to ``sql`` by substituting each table reference.

    Raises :class:`PolicyError` if the statement touches a table with no
    grant, or writes to a table with read-only access.
    """
    tree = _parse_statement(sql)
    kind = "read" if isinstance(tree, (exp.Select, exp.SetOperation)) else "write"
    known = {t.casefold(): t for t in known_tables}
    references = list(tree.find_all(exp.Table))
    if not references:
        raise PolicyError("query does not reference a known table")
    changed = False
    for reference in references:
        if (not isinstance(reference.this, exp.Identifier) or reference.catalog
                or reference.db.casefold() not in ("", "main")):
            raise PolicyError("only local named tables are supported")
        table = known.get(reference.name.casefold())
        if table is None:
            raise PolicyError(f"unknown table {reference.name!r}")
        grant = grants.get(table)
        if grant is None:
            raise PolicyError(f"no policy grants access to table {table!r}")
        if grant.access == "full":
            continue
        if kind != "read":
            raise PolicyError(f"table {table!r} is read-only under the effective policy")

        cols = [exp.column(c) for c in grant.columns] if grant.columns else [exp.Star()]
        derived = exp.select(*cols).from_(exp.Table(this=exp.to_identifier(table)))
        if grant.row_filter:
            try:
                derived = derived.where(sqlglot.parse_one(grant.row_filter, read="sqlite"))
            except SqlglotError as exc:
                raise PolicyError("invalid configured row filter") from exc
        alias = reference.args.get("alias")
        alias = alias.copy() if alias else exp.TableAlias(this=reference.this.copy())
        reference.replace(exp.Subquery(this=derived, alias=alias))
        changed = True
    return tree.sql(dialect="sqlite") if changed else sql.strip().rstrip(";")
