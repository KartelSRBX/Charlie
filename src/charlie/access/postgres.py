"""PostgreSQL side of access-as-code: the same model, compiled for a different engine.

PostgreSQL has no marker for "created by the model", so every role is tagged with a comment
(`charlie:group`, `charlie:service`, `charlie:person`) and everything that is not superuser or
built in counts as managed: a role somebody created by hand shows up as drift, like in ClickHouse.

Unlike ClickHouse, DDL is transactional here, so `apply` is all or nothing.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

import psycopg

from ..topology import Topology
from .facts import Fact, Op, Plan, diff
from .model import AccessModel

KINDS = ("role", "member", "grant")

NOLOGIN = "nologin"
SCRAM = "login:scram"
EXTERNAL = "login:external"  # no password in the database: the pg_hba rule asks the directory
OBJ_SQL = {"schema": "SCHEMA", "table": "TABLE", "sequence": "SEQUENCE"}
DEFAULT_SQL = {"default_table": "TABLES", "default_sequence": "SEQUENCES"}
DEFAULT_OBJ = {"r": "default_table", "S": "default_sequence"}


class UnmanageableError(Exception):
    """The model refers to something that is not there, or cannot express what is there."""


def qi(name: str) -> str:
    """Quote an identifier. PUBLIC is a keyword here, not a role name."""
    if name == "PUBLIC":
        return name
    return '"' + name.replace('"', '""') + '"'


def ql(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


@dataclass(frozen=True)
class Catalog:
    """The objects that exist in the schemas the model covers."""

    tables: dict[str, list[str]]  # schema -> tables, views, ...
    sequences: dict[str, list[str]]

    @classmethod
    def empty(cls, schemas: list[str]) -> Catalog:
        return cls({s: [] for s in schemas}, {s: [] for s in schemas})


def desired_facts(
    model: AccessModel,
    catalog: Catalog,
    people: dict[str, set[str]] | None = None,
) -> set[Fact]:
    """What the model says PostgreSQL should contain.

    `people` maps a login name to the roles the directory gives them. ``None`` leaves people out.
    """
    facts: set[Fact] = set()
    base = {r.name: r for r in model.base_roles()}

    for name, role in base.items():
        lvl = model.levels[role.level]
        facts.add(Fact("role", (name,), (NOLOGIN, "group")))
        schema_privs = sorted(set(lvl.postgres_schema))
        can_write = "INSERT" in lvl.postgres
        for schema in model.domains[role.domain].postgres.schemas:
            for priv in schema_privs:
                facts.add(Fact("grant", (name, priv, "schema", schema)))
            for priv in lvl.postgres:
                for tbl in catalog.tables.get(schema, []):
                    facts.add(Fact("grant", (name, priv, "table", f"{schema}.{tbl}")))
                facts.add(Fact("grant", (name, priv, "default_table", schema)))
            if can_write:
                for priv in ("SELECT", "USAGE"):
                    for seq in catalog.sequences.get(schema, []):
                        facts.add(Fact("grant", (name, priv, "sequence", f"{schema}.{seq}")))
                    facts.add(Fact("grant", (name, priv, "default_sequence", schema)))

    for acct, spec in model.service_accounts.items():
        facts.add(Fact("role", (acct,), (SCRAM, "service")))
        for r in spec.roles:
            facts.add(Fact("member", (acct, r)))

    for person, roles in sorted((people or {}).items()):
        wanted = sorted(r for r in roles if r in base)
        if not wanted:
            continue
        facts.add(Fact("role", (person,), (EXTERNAL, "person")))
        for r in wanted:
            facts.add(Fact("member", (person, r)))
    return facts


def connect(topo: Topology) -> psycopg.Connection:
    pg = topo.postgres
    if pg is None:
        raise UnmanageableError("topology.yaml has no postgres section")
    return psycopg.connect(
        host=os.environ.get("PG_HOST", "localhost"),
        port=pg.port,
        dbname=pg.database,
        user=os.environ.get("PG_ADMIN_USER", "postgres"),
        password=os.environ.get("PG_ADMIN_PASSWORD", "charlie"),
        connect_timeout=5,
        autocommit=True,
    )


class PostgresAccess:
    name = "postgres"

    def __init__(
        self,
        conn: psycopg.Connection,
        model: AccessModel,
        secrets: dict[str, str] | None = None,
        people: dict[str, set[str]] | None = None,
    ):
        self.conn = conn
        self.model = model
        self.secrets = secrets or {}
        self.people = people
        self._person_roles: set[str] = set()

    # --- catalog ---------------------------------------------------------------------------
    @property
    def schemas(self) -> list[str]:
        return sorted({s for d in self.model.domains.values() for s in d.postgres.schemas})

    def _rows(self, sql: str, *params) -> list[tuple]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def catalog(self) -> Catalog:
        have = {r[0] for r in self._rows("SELECT nspname FROM pg_namespace")}
        missing = [s for s in self.schemas if s not in have]
        if missing:
            raise UnmanageableError(f"schemas in the model do not exist in PostgreSQL: {missing}")
        tables: dict[str, list[str]] = {s: [] for s in self.schemas}
        seqs: dict[str, list[str]] = {s: [] for s in self.schemas}
        for schema, kind, name in self._rows(
            "SELECT n.nspname, c.relkind::text, c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = ANY(%s) AND c.relkind IN ('r','p','v','m','f','S') "
            "AND NOT c.relispartition ORDER BY 1, 3",
            self.schemas,
        ):
            (seqs if kind == "S" else tables)[schema].append(name)
        return Catalog(tables, seqs)

    # --- desired / observed ----------------------------------------------------------------
    def desired(self) -> set[Fact]:
        return desired_facts(self.model, self.catalog(), self.people)

    def observe(self) -> set[Fact]:
        facts: set[Fact] = set()
        self._person_roles = set()
        for name, login, has_pw, scram, comment in self._rows(
            "SELECT r.rolname, r.rolcanlogin, a.rolpassword IS NOT NULL, "
            "coalesce(a.rolpassword LIKE 'SCRAM-SHA-256$%%', false), "
            "coalesce(shobj_description(r.oid, 'pg_authid'), '') "
            "FROM pg_roles r JOIN pg_authid a ON a.oid = r.oid "
            "WHERE r.rolname !~ '^pg_' AND NOT r.rolsuper"
        ):
            if not login:
                auth = NOLOGIN
            elif not has_pw:
                auth = EXTERNAL
            else:
                auth = SCRAM if scram else "login:other"
            marker = comment.removeprefix("charlie:") if comment.startswith("charlie:") else ""
            if marker == "person":
                self._person_roles.add(name)
            facts.add(Fact("role", (name,), (auth, marker)))
        # People are only compared when a directory was supplied; otherwise they are not ours.
        if self.people is None:
            facts = {f for f in facts if f.attrs[1] != "person"}

        for member, role in self._rows(
            "SELECT m.rolname, g.rolname FROM pg_auth_members am "
            "JOIN pg_roles m ON m.oid = am.member JOIN pg_roles g ON g.oid = am.roleid "
            "WHERE m.rolname !~ '^pg_' AND g.rolname !~ '^pg_' "
            "AND NOT m.rolsuper AND NOT g.rolsuper"
        ):
            if self.people is None and member in self._person_roles:
                continue
            facts.add(Fact("member", (member, role)))

        grantee = "coalesce(g.rolname, 'PUBLIC')"
        join = "LEFT JOIN pg_roles g ON g.oid = x.grantee"
        keep = "AND (g.oid IS NULL OR (NOT g.rolsuper AND g.rolname !~ '^pg_'))"
        for schema, who, priv in self._rows(
            f"SELECT n.nspname, {grantee}, x.privilege_type FROM pg_namespace n "
            f"CROSS JOIN LATERAL aclexplode(n.nspacl) x {join} "
            f"WHERE n.nspname = ANY(%s) AND x.grantee <> n.nspowner {keep}",
            self.schemas,
        ):
            facts.add(Fact("grant", (who, priv, "schema", schema)))
        for schema, name, kind, who, priv in self._rows(
            f"SELECT n.nspname, c.relname, c.relkind::text, {grantee}, x.privilege_type "
            f"FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            f"CROSS JOIN LATERAL aclexplode(c.relacl) x {join} "
            f"WHERE n.nspname = ANY(%s) AND c.relkind IN ('r','p','v','m','f','S') "
            f"AND x.grantee <> c.relowner {keep}",
            self.schemas,
        ):
            obj = "sequence" if kind == "S" else "table"
            facts.add(Fact("grant", (who, priv, obj, f"{schema}.{name}")))
        for schema, kind, who, priv in self._rows(
            f"SELECT n.nspname, d.defaclobjtype::text, {grantee}, x.privilege_type "
            f"FROM pg_default_acl d JOIN pg_namespace n ON n.oid = d.defaclnamespace "
            f"CROSS JOIN LATERAL aclexplode(d.defaclacl) x {join} "
            f"WHERE n.nspname = ANY(%s) AND d.defaclobjtype IN ('r','S') "
            f"AND d.defaclrole = (SELECT oid FROM pg_roles WHERE rolname = current_user) "
            f"AND x.grantee <> d.defaclrole {keep}",
            self.schemas,
        ):
            facts.add(Fact("grant", (who, priv, DEFAULT_OBJ[kind], schema)))
        return facts

    # --- plan / apply ----------------------------------------------------------------------
    def plan(self) -> Plan:
        desired = self.desired()
        return diff(desired, self.observe(), KINDS)

    def directory_owned(self, fact: Fact) -> bool:
        """Facts about people follow the directory, so removing them does not need --prune."""
        if self.people is None:
            return False
        if fact.kind == "role":
            return fact.attrs[1:2] == ("person",)
        if fact.kind == "member":
            return fact.key[0] in self._person_roles or fact.key[0] in self.people
        return False

    def _password_hash(self, user: str) -> str:
        pw = self.secrets[user]
        return self.conn.pgconn.encrypt_password(
            pw.encode(), user.encode(), b"scram-sha-256"
        ).decode()

    def _login_clause(self, f: Fact, reveal: bool) -> str:
        auth = f.attrs[0]
        if auth == SCRAM:
            h = self._password_hash(f.key[0]) if reveal and f.key[0] in self.secrets else "<hidden>"
            return f"LOGIN PASSWORD {ql(h)}"
        if auth == EXTERNAL:
            return "LOGIN PASSWORD NULL"
        return "NOLOGIN PASSWORD NULL"

    def statements(self, op: Op, *, reveal: bool = False) -> list[str]:
        f = op.fact
        if op.action == "remove":
            return _remove_sql(f)
        if f.kind == "role":
            name = qi(f.key[0])
            verb = "ALTER" if op.action == "change" else "CREATE"
            return [
                f"{verb} ROLE {name} {self._login_clause(f, reveal)}",
                f"COMMENT ON ROLE {name} IS {ql('charlie:' + f.attrs[1])}",
            ]
        if f.kind == "member":
            return [f"GRANT {qi(f.key[1])} TO {qi(f.key[0])}"]
        who, priv, obj, target = f.key
        return [_grant_sql("GRANT", "TO", who, priv, obj, target)]

    def describe(self, op: Op) -> str:
        f = op.fact
        sign = {"add": "+", "change": "~", "remove": "-"}[op.action]
        extra = ""
        if op.action == "remove":
            extra = "   (configured, but not in the model)"
            if self.directory_owned(f):
                extra = "   (the directory no longer grants it)"
        if f.kind == "member":
            what = f"membership {f.key[0]} in {f.key[1]}"
        elif f.kind == "grant":
            who, priv, obj, target = f.key
            what = f"grant {priv} ON {obj.replace('_', ' ')} {target} TO {who}"
        else:
            what = f"role {f.key[0]} ({f.attrs[0]}, {f.attrs[1] or 'not from the model'})"
            if op.action == "change" and op.before is not None:
                extra = f"   (was {op.before.attrs[0]}, {op.before.attrs[1] or 'unmarked'})"
        return f"{sign} {what}{extra}"

    def apply(self, plan: Plan, *, prune: bool = False, log=print) -> int:
        """Run the plan in one transaction. Returns the number of removals skipped."""
        skipped = 0
        with self.conn.transaction(), self.conn.cursor() as cur:
            for op in plan.ops:
                if op.action == "remove" and not prune and not self.directory_owned(op.fact):
                    skipped += 1
                    continue
                for sql in self.statements(op, reveal=True):
                    log(f"  {_redact(sql)}")
                    cur.execute(sql)
        return skipped


def _grant_sql(verb: str, prep: str, who: str, priv: str, obj: str, target: str) -> str:
    if obj in DEFAULT_SQL:
        return (
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {qi(target)} "
            f"{verb} {priv} ON {DEFAULT_SQL[obj]} {prep} {qi(who)}"
        )
    name = ".".join(qi(p) for p in target.split("."))
    return f"{verb} {priv} ON {OBJ_SQL[obj]} {name} {prep} {qi(who)}"


def _remove_sql(f: Fact) -> list[str]:
    if f.kind == "role":
        name = f.key[0]
        return [
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = {ql(name)}",
            f"DROP ROLE IF EXISTS {qi(name)}",
        ]
    if f.kind == "member":
        return [f"REVOKE {qi(f.key[1])} FROM {qi(f.key[0])}"]
    who, priv, obj, target = f.key
    return [_grant_sql("REVOKE", "FROM", who, priv, obj, target)]


def _redact(sql: str) -> str:
    return re.sub(r"(PASSWORD )'[^']*'", r"\1'<hidden>'", sql)


__all__ = [
    "KINDS",
    "Catalog",
    "PostgresAccess",
    "UnmanageableError",
    "connect",
    "desired_facts",
]
