import json
import unittest
from pathlib import Path

from apps.client import GatewayClient, GatewayError
from apps.comp_table_cli import render_table, search
from apps.comp_table_export import CompensationExporter
from enforcement.db import seeded_connection
from enforcement.gateway import GatewayState, start_background
from enforcement.identity import Directory, User
from enforcement.policies import PolicyError, PolicySet, TableGrant, classify_statement, rewrite
from enforcement.resource import AccessDenied, EnforcedResource

ROOT = Path(__file__).resolve().parent.parent
POLICIES = PolicySet.load(ROOT / "policies.json")
QUERY = "SELECT id, name, team, role, salary, last_raise FROM employees ORDER BY name"

NINA = User("nina", "Nina", ("hr",))
TOM = User("tom", "Tom", ("eng-managers",))
AVA = User("ava", "Ava", ("marketing",))


def resource() -> EnforcedResource:
    return EnforcedResource(seeded_connection(), POLICIES)


class SeedTests(unittest.TestCase):
    def test_fifteen_employees_five_engineering(self):
        conn = seeded_connection()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM employees").fetchone()[0], 15)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM employees WHERE team='Engineering'").fetchone()[0], 5)


class PolicyTests(unittest.TestCase):
    def test_effective_grants(self):
        self.assertEqual(POLICIES.effective_grants(("hr",))["employees"].access, "full")
        g = POLICIES.effective_grants(("eng-managers",))["employees"]
        self.assertEqual(g.access, "read")
        self.assertEqual(g.row_filter, "team = 'Engineering'")
        self.assertEqual(POLICIES.effective_grants(("marketing",)), {})

    def test_merge_most_permissive(self):
        a = TableGrant("read", "team = 'A'", ("id",))
        b = TableGrant("read", "team = 'B'", ("name",))
        m = a.merged_with(b)
        self.assertEqual(m.row_filter, "(team = 'A') OR (team = 'B')")
        self.assertEqual(m.columns, ("id", "name"))
        self.assertEqual(a.merged_with(TableGrant("full")).access, "full")

    def test_rewrite_wraps_table(self):
        grants = {"employees": TableGrant("read", "team = 'Engineering'", ("id", "name", "team"))}
        out = rewrite("SELECT name FROM employees WHERE salary > 1", grants, ["employees"])
        self.assertIn("(SELECT id, name, team FROM employees WHERE team = 'Engineering') AS employees", out)
        self.assertTrue(out.endswith("WHERE salary > 1"))

    def test_rewrite_keeps_alias(self):
        grants = {"employees": TableGrant("read", "team = 'Engineering'")}
        out = rewrite("SELECT e.name FROM employees e JOIN employees AS f ON e.id = f.id", grants, ["employees"])
        self.assertIn(") AS e JOIN", out)
        self.assertIn(") AS f ON", out)

    def test_rewrite_rejects_write_on_read_grant(self):
        grants = {"employees": TableGrant("read")}
        with self.assertRaises(PolicyError):
            rewrite("DELETE FROM employees", grants, ["employees"])

    def test_rewrite_rejects_unknown_table_and_multi_statement(self):
        with self.assertRaises(PolicyError):
            rewrite("SELECT 1", {}, ["employees"])
        with self.assertRaises(PolicyError):
            classify_statement("SELECT 1; DROP TABLE employees")

    def test_comment_hiding_does_not_help(self):
        grants = {"employees": TableGrant("read", "team = 'Engineering'")}
        out = rewrite("SELECT * FROM /* x */ employees -- trailing", grants, ["employees"])
        self.assertIn("WHERE team = 'Engineering'", out)


class ResourceTests(unittest.TestCase):
    def test_hr_sees_everything_unrewritten(self):
        r = resource().query(NINA, QUERY)
        self.assertEqual(len(r), 15)
        self.assertFalse(r.narrowed)
        self.assertEqual(r.policies_applied, ["HR full access"])

    def test_manager_is_narrowed_to_team(self):
        r = resource().query(TOM, QUERY)
        self.assertEqual(len(r), 5)
        self.assertTrue(r.narrowed)
        self.assertTrue(all(row["team"] == "Engineering" for row in r.rows))

    def test_manager_cannot_escape_with_or_true(self):
        r = resource().query(TOM, "SELECT * FROM employees WHERE team = 'Sales' OR 1=1")
        self.assertEqual(len(r), 5)

    def test_manager_cannot_write(self):
        res = resource()
        with self.assertRaises(AccessDenied):
            res.query(TOM, "UPDATE employees SET salary = 1")
        self.assertEqual(res.conn.execute("SELECT MIN(salary) FROM employees").fetchone()[0], 72000)

    def test_all_join_references_are_filtered(self):
        queries = [
            'SELECT leaked.team FROM employees e JOIN "employees" leaked ON 1=1',
            'SELECT leaked.team FROM employees e, employees leaked',
            'SELECT leaked.team FROM employees e JOIN main.employees leaked ON 1=1',
            'SELECT leaked.team FROM [employees] e JOIN `employees` leaked ON 1=1',
        ]
        for sql in queries:
            with self.subTest(sql=sql):
                res = resource()
                try:
                    result = res.query(TOM, sql)
                    self.assertEqual(len(result), 25)
                    self.assertEqual({row['team'] for row in result.rows}, {'Engineering'})
                finally:
                    res.conn.close()

    def test_nested_reads_and_unions_are_filtered(self):
        queries = [
            'SELECT team FROM employees UNION ALL SELECT team FROM "employees"',
            'SELECT team FROM (SELECT team FROM main.employees)',
            "SELECT team FROM employees WHERE name != '-- not a comment'",
            "SELECT team FROM employees WHERE name != '; not a statement'",
        ]
        for sql in queries:
            with self.subTest(sql=sql):
                res = resource()
                try:
                    self.assertEqual({r['team'] for r in res.query(TOM, sql).rows}, {'Engineering'})
                finally:
                    res.conn.close()

    def test_cte_writes_are_rejected_without_mutation(self):
        res = resource()
        try:
            before = list(res.conn.execute('SELECT * FROM employees'))
            with self.assertRaises(AccessDenied):
                res.query(TOM, 'WITH c AS (SELECT 1) UPDATE employees SET salary = 1 WHERE id = 6')
            self.assertEqual(list(res.conn.execute('SELECT * FROM employees')), before)
        finally:
            res.conn.close()

    def test_unknown_tables_and_database_commands_are_rejected(self):
        queries = [
            'SELECT x.name FROM employees e JOIN sqlite_master x ON 1=1',
            'SELECT x.name FROM employees e, sqlite_master x',
            'SELECT * FROM temp.employees',
            'EXPLAIN UPDATE employees SET salary = 1 WHERE id = 6',
            'PRAGMA table_info(employees)',
            'WITH e AS (SELECT * FROM employees) SELECT * FROM e',
        ]
        res = resource()
        try:
            for sql in queries:
                with self.subTest(sql=sql), self.assertRaises(AccessDenied):
                    res.query(TOM, sql)
        finally:
            res.conn.close()

    def test_disallowed_column_is_never_returned(self):
        policy = PolicySet.from_dict({'resource': 'test', 'enforced': True, 'policies': [
            {'name': 'limited', 'groups': ['eng-managers'], 'tables': {'employees': {
                'access': 'read', 'columns': ['name'], 'row_filter': "team = 'Engineering'"}}}
        ]})
        res = EnforcedResource(seeded_connection(), policy)
        try:
            result = res.query(TOM, 'SELECT * FROM "employees"')
            self.assertEqual(result.columns, ['name'])
            self.assertEqual(len(result), 5)
            with self.assertRaises(AccessDenied):
                res.query(TOM, 'SELECT e.salary FROM employees e')
        finally:
            res.conn.close()

    def test_hr_can_write(self):
        res = resource()
        res.query(NINA, "UPDATE employees SET salary = salary + 1 WHERE id = 1")
        self.assertEqual(res.conn.execute("SELECT salary FROM employees WHERE id = 1").fetchone()[0], 187001)

    def test_uncovered_user_is_blocked_and_audited(self):
        res = resource()
        with self.assertRaises(AccessDenied):
            res.query(AVA, QUERY)
        entries = res.audit.entries("ava")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].outcome, "blocked")
        self.assertIsNone(entries[0].executed_sql)

    def test_unenforced_resource_lets_everything_through(self):
        ps = PolicySet.from_dict({**json.loads((ROOT / "policies.json").read_text()), "enforced": False})
        r = EnforcedResource(seeded_connection(), ps).query(AVA, QUERY)
        self.assertEqual(len(r), 15)

    def test_audit_summary_counts(self):
        res = resource()
        res.query(NINA, QUERY)
        res.query(TOM, QUERY)
        try:
            res.query(AVA, QUERY)
        except AccessDenied:
            pass
        self.assertEqual(res.audit.summary(), {"allowed": 1, "narrowed": 1, "blocked": 1})


class IdentityTests(unittest.TestCase):
    def test_token_roundtrip_and_tamper(self):
        d = Directory(secret=b"test-secret")
        user = d.authenticate("tom", "tom-demo")
        self.assertIsNotNone(user)
        token = d.issue_token(user)
        self.assertEqual(d.verify_token(token).username, "tom")
        self.assertIsNone(d.verify_token(token[:-2] + "zz"))
        self.assertIsNone(d.verify_token("garbage"))
        self.assertIsNone(d.authenticate("tom", "wrong"))

    def test_expired_token_rejected(self):
        d = Directory(secret=b"k", ttl_seconds=-1)
        self.assertIsNone(d.verify_token(d.issue_token(d.get("nina"))))


class GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.resource = resource()
        cls.server, cls.url = start_background(GatewayState(cls.resource, Directory(secret=b"gw")))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_three_apps_agree_per_user(self):
        for user, pw, expected in [("nina", "nina-demo", 15), ("tom", "tom-demo", 5)]:
            rows = GatewayClient(self.url).login(user, pw).query()["rows"]
            csv_rows = CompensationExporter(user, pw, self.url).export("csv").count("\n") - 1
            self.assertEqual(len(rows), expected)
            self.assertEqual(csv_rows, expected)
            self.assertEqual(len(search(rows, "")), expected)
        self.assertIn("SALARY", render_table(rows))

    def test_blocked_user_gets_403(self):
        with self.assertRaises(GatewayError) as ctx:
            GatewayClient(self.url).login("ava", "ava-demo").query()
        self.assertEqual(ctx.exception.status, 403)
        self.assertTrue(ctx.exception.blocked)

    def test_identity_comes_from_token_not_payload(self):
        c = GatewayClient(self.url).login("tom", "tom-demo")
        out = c.query("SELECT * FROM employees", params=[])
        self.assertEqual(out["row_count"], 5)
        self.assertTrue(out["narrowed"])

    def test_login_required_and_bad_resource(self):
        with self.assertRaises(GatewayError) as ctx:
            GatewayClient(self.url).query()
        self.assertEqual(ctx.exception.status, 401)
        with self.assertRaises(GatewayError) as ctx:
            GatewayClient(self.url, resource="other_db").login("nina", "nina-demo").query()
        self.assertEqual(ctx.exception.status, 404)

    def test_audit_restricted_to_hr(self):
        self.assertIn("summary", GatewayClient(self.url).login("nina", "nina-demo").audit())
        with self.assertRaises(GatewayError) as ctx:
            GatewayClient(self.url).login("tom", "tom-demo").audit()
        self.assertEqual(ctx.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
