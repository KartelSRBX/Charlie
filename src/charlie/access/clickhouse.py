"""ClickHouse side of access-as-code: model -> desired facts, server -> observed facts, SQL.

Only access entities stored in the *replicated* directory (Keeper) are managed. The bootstrap
admin in users.xml and people who log in through LDAP live elsewhere and are not touched.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable

from clickhouse_connect.driver.client import Client

from .facts import Fact, Op, Plan, diff
from .model import AccessModel, Setting

KINDS = ("role", "user", "member", "grant", "policy", "quota", "profile", "unrecognized")

_GRANT_ON = re.compile(r"^GRANT (?P<privs>.+?) ON (?P<target>\S+) TO (?P<who>\S+)$")
_GRANT_ROLE = re.compile(r"^GRANT (?P<roles>.+?) TO (?P<who>\S+)$")


class UnmanageableError(Exception):
    """Something exists that cannot be described by the model and cannot be removed by it."""


def unquote(name: str) -> str:
    return name.strip("`")


def q(name: str) -> str:
    """Quote an identifier. Names are validated against a strict pattern long before this."""
    return "`" + name.replace("`", "") + "`"


def scope(apply_all: bool, to: list[str], except_: list[str]) -> str:
    if apply_all:
        return "all_except:" + ",".join(sorted(except_))
    return "to:" + ",".join(sorted(to))


def parse_show_grants(grantee: str, lines: list[str]) -> set[Fact]:
    """Turn the output of ``SHOW GRANTS FOR grantee`` into facts."""
    out: set[Fact] = set()
    for line in lines:
        line = line.strip()
        if m := _GRANT_ON.match(line):
            if "WITH GRANT OPTION" in line or (m["who"] != q(grantee) and m["who"] != grantee):
                out.add(Fact("unrecognized", (grantee, line)))
                continue
            for priv in m["privs"].split(", "):
                out.add(Fact("grant", (grantee, priv, unquote(m["target"]))))
        elif m := _GRANT_ROLE.match(line):
            if "WITH ADMIN OPTION" in line:
                out.add(Fact("unrecognized", (grantee, line)))
                continue
            for role in m["roles"].split(", "):
                out.add(Fact("member", (grantee, unquote(role))))
        else:
            out.add(Fact("unrecognized", (grantee, line)))
    return out


def _norm_setting_value(v) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v)


def settings_attrs(settings: dict) -> tuple:
    rows = []
    for name, raw in sorted(settings.items()):
        s = Setting.model_validate(raw if isinstance(raw, dict) else {"value": raw})
        rows.append(
            (
                name,
                _norm_setting_value(s.value),
                "" if s.min is None else _norm_setting_value(s.min),
                "" if s.max is None else _norm_setting_value(s.max),
                "CONST" if s.readonly else "",
            )
        )
    return tuple(rows)


def sha256_hex(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


LDAP = "ldap"
LDAP_SERVER = "directory"  # the <ldap_servers> entry in cluster.xml


def desired_facts(
    model: AccessModel,
    normalize_filter: Callable[[str, str], str],
    people: dict[str, set[str]] | None = None,
) -> set[Fact]:
    """What the model says ClickHouse should contain.

    `people` maps a login to the roles the directory gives them; ``None`` leaves people out.
    """
    facts: set[Fact] = set()
    roles = {r.name: r for r in model.derived_roles()}
    scoped = {s.name: s for s in model.scoped_roles}

    for name, role in roles.items():
        facts.add(Fact("role", (name,)))
        lvl = model.levels[role.level]
        base = scoped[name].base if name in scoped else name
        base_role = roles[base]
        for db in model.domains[base_role.domain].clickhouse.databases:
            for priv in lvl.clickhouse:
                facts.add(Fact("grant", (name, priv, f"{db}.*")))

    # Row policies. Once a table has any policy, users matched by none of them see no rows, so
    # every table with a scoped policy also gets a policy that lets everybody else through.
    restricted: dict[str, set[str]] = {}
    for sr in model.scoped_roles:
        for table, flt in sr.row_policies.items():
            restricted.setdefault(table, set()).add(sr.name)
            short = f"{sr.name}__{table.partition('.')[2]}"
            facts.add(
                Fact("policy", (short, table), (normalize_filter(table, flt), f"to:{sr.name}"))
            )
    for table, names in restricted.items():
        short = f"allow_all__{table.replace('.', '__')}"
        facts.add(
            Fact(
                "policy",
                (short, table),
                (normalize_filter(table, "1"), scope(True, [], list(names))),
            )
        )

    for lvl_name, lvl in model.levels.items():
        holders = [r.name for r in roles.values() if r.level == lvl_name]
        if lvl.profile:
            facts.add(
                Fact(
                    "profile",
                    (lvl.profile,),
                    (settings_attrs(model.profiles[lvl.profile]), scope(False, holders, [])),
                )
            )
        if lvl.quota:
            qt = model.quotas[lvl.quota]
            facts.add(
                Fact(
                    "quota",
                    (lvl.quota,),
                    (qt.seconds, tuple(sorted(qt.limits.items())), scope(False, holders, [])),
                )
            )

    for acct, spec in model.service_accounts.items():
        facts.add(Fact("user", (acct,), ("sha256_password",)))
        for r in spec.roles:
            facts.add(Fact("member", (acct, r)))

    for person, wanted in sorted((people or {}).items()):
        granted = sorted(r for r in wanted if r in roles)
        if not granted:
            continue
        facts.add(Fact("user", (person,), (LDAP,)))
        for r in granted:
            facts.add(Fact("member", (person, r)))
    return facts


class ClickhouseAccess:
    name = "clickhouse"

    def __init__(
        self,
        client: Client,
        model: AccessModel,
        secrets: dict[str, str] | None = None,
        people: dict[str, set[str]] | None = None,
    ):
        self.client = client
        self.model = model
        self.secrets = secrets or {}
        self.people = people
        self._ldap_users: set[str] = set()

    # --- desired ---------------------------------------------------------------------------
    def normalize_filter(self, table: str, expr: str) -> str:
        """Let the server print the expression the way it stores it, so the two compare equal."""
        probe = f"CREATE ROW POLICY p ON {table} FOR SELECT USING {expr} TO r"
        out = self.client.query(
            "SELECT formatQuerySingleLine({p:String})", parameters={"p": probe}
        ).result_rows[0][0]
        m = re.match(r"^CREATE ROW POLICY p ON \S+ FOR SELECT USING (.*) TO r$", out, re.S)
        if not m:
            raise UnmanageableError(f"cannot normalise filter {expr!r}: {out}")
        return m[1]

    def check_policy_tables(self) -> None:
        """A row policy on a Distributed table does nothing: refuse it instead of shipping it."""
        tables = {t for sr in self.model.scoped_roles for t in sr.row_policies}
        for table in sorted(tables):
            db, _, name = table.partition(".")
            rows = self.client.query(
                "SELECT engine FROM system.tables "
                "WHERE database = {d:String} AND name = {t:String}",
                parameters={"d": db, "t": name},
            ).result_rows
            if not rows:
                raise UnmanageableError(f"row policy target {table} does not exist (migrate first)")
            if rows[0][0] == "Distributed":
                raise UnmanageableError(
                    f"{table} is a Distributed table, and a row policy on it has no effect: "
                    f"the shards run the query as the calling user and never see it. "
                    f"Put the policy on the table that stores the data, e.g. {table}_local"
                )

    def desired(self) -> set[Fact]:
        self.check_policy_tables()
        return desired_facts(self.model, self.normalize_filter, self.people)

    # --- observed --------------------------------------------------------------------------
    def observe(self) -> set[Fact]:
        c = self.client
        facts: set[Fact] = set()
        grantees: list[str] = []
        self._ldap_users = set()
        for (name,) in c.query(
            "SELECT name FROM system.roles WHERE storage = 'replicated'"
        ).result_rows:
            facts.add(Fact("role", (name,)))
            grantees.append(name)
        for name, auth in c.query(
            "SELECT name, toString(auth_type) FROM system.users WHERE storage = 'replicated'"
        ).result_rows:
            if auth == LDAP:
                self._ldap_users.add(name)
                if self.people is None:  # not ours unless a directory was supplied
                    continue
            facts.add(Fact("user", (name,), (auth,)))
            grantees.append(name)
        for who in grantees:
            rows = c.query(f"SHOW GRANTS FOR {q(who)}").result_rows
            facts |= parse_show_grants(who, [r[0] for r in rows])

        for short, db, tbl, flt, restrictive, all_, to, ex in c.query(
            "SELECT short_name, database, table, select_filter, is_restrictive, apply_to_all, "
            "apply_to_list, apply_to_except FROM system.row_policies WHERE storage = 'replicated'"
        ).result_rows:
            sc = scope(bool(all_), list(to), list(ex))
            if restrictive:
                sc = "restrictive:" + sc
            facts.add(Fact("policy", (short, f"{db}.{tbl}"), (flt, sc)))

        limits = {
            (n, int(d)): {k: v for k, v in zip(LIMIT_COLS, vals, strict=True) if v is not None}
            for n, d, *vals in c.query(
                f"SELECT quota_name, duration, {', '.join(LIMIT_COLS)} FROM system.quota_limits"
            ).result_rows
        }
        for name, durations, all_, to, ex in c.query(
            "SELECT name, durations, apply_to_all, apply_to_list, apply_to_except "
            "FROM system.quotas WHERE storage = 'replicated'"
        ).result_rows:
            # A quota with several intervals cannot come from the model: report it as such.
            dur = int(durations[0]) if len(durations) == 1 else tuple(map(int, durations))
            lim = limits.get((name, int(durations[0])), {}) if len(durations) == 1 else {}
            facts.add(
                Fact(
                    "quota",
                    (name,),
                    (dur, tuple(sorted(lim.items())), scope(bool(all_), list(to), list(ex))),
                )
            )

        elements: dict[str, list] = {}
        for prof, setting, value, mn, mx, writ in c.query(
            "SELECT profile_name, setting_name, value, min, max, toString(writability) "
            "FROM system.settings_profile_elements WHERE user_name IS NULL AND role_name IS NULL "
            "AND setting_name IS NOT NULL ORDER BY profile_name, index"
        ).result_rows:
            writ = "CONST" if writ == "CONST" else ""
            elements.setdefault(prof, []).append(
                (
                    setting,
                    str(value),
                    "" if mn is None else str(mn),
                    "" if mx is None else str(mx),
                    writ,
                )
            )
        for name, all_, to, ex in c.query(
            "SELECT name, apply_to_all, apply_to_list, apply_to_except "
            "FROM system.settings_profiles WHERE storage = 'replicated'"
        ).result_rows:
            facts.add(
                Fact(
                    "profile",
                    (name,),
                    (tuple(sorted(elements.get(name, []))), scope(bool(all_), list(to), list(ex))),
                )
            )
        return facts

    # --- plan / apply ----------------------------------------------------------------------
    def plan(self) -> Plan:
        return diff(self.desired(), self.observe(), KINDS)

    def statements(self, op: Op, *, reveal: bool = False) -> list[str]:
        f = op.fact
        k = f.kind
        if op.action == "remove":
            return _remove_sql(f)
        change = op.action == "change"
        if k == "role":
            return [f"CREATE ROLE IF NOT EXISTS {q(f.key[0])}"]
        if k == "user" and f.attrs == (LDAP,):
            verb = "ALTER" if change else "CREATE"
            return [f"{verb} USER {q(f.key[0])} IDENTIFIED WITH ldap SERVER '{LDAP_SERVER}'"]
        if k == "user":
            h = (
                sha256_hex(self.secrets[f.key[0]])
                if reveal and f.key[0] in self.secrets
                else "<hidden>"
            )
            verb = "ALTER" if change else "CREATE"
            return [f"{verb} USER {q(f.key[0])} IDENTIFIED WITH sha256_hash BY '{h}'"]
        if k == "member":
            return [f"GRANT {q(f.key[1])} TO {q(f.key[0])}"]
        if k == "grant":
            who, priv, target = f.key
            return [f"GRANT {priv} ON {target} TO {q(who)}"]
        if k == "policy":
            name, table = f.key
            flt, sc = f.attrs
            verb = "ALTER ROW POLICY" if change else "CREATE ROW POLICY"
            return [f"{verb} {q(name)} ON {table} FOR SELECT USING {flt} TO {_to_clause(sc)}"]
        if k == "quota":
            name = f.key[0]
            secs, lim, sc = f.attrs
            limits = ", ".join(f"{n.removeprefix('max_')} = {v}" for n, v in lim)
            return [
                f"CREATE QUOTA {'OR REPLACE ' if change else ''}{q(name)} "
                f"FOR INTERVAL {secs} second MAX {limits} TO {_to_clause(sc)}"
            ]
        if k == "profile":
            name = f.key[0]
            rows, sc = f.attrs
            return [
                f"CREATE SETTINGS PROFILE {'OR REPLACE ' if change else ''}{q(name)} "
                f"SETTINGS {_settings_sql(rows)} TO {_to_clause(sc)}"
            ]
        raise UnmanageableError(f"cannot create {k} {f.key}")

    def describe(self, op: Op) -> str:
        f = op.fact
        sign = {"add": "+", "change": "~", "remove": "-"}[op.action]
        extra = ""
        if op.action == "remove":
            extra = "   (configured, but not in the model)"
            if self.directory_owned(f):
                extra = "   (the directory no longer grants it)"
        if op.action == "change" and op.before is not None:
            extra = f"   (was {op.before.attrs}, now {f.attrs})"
        if f.kind == "member":
            what = f"membership {f.key[0]} in {f.key[1]}"
        elif f.kind == "grant":
            what = f"grant {f.key[1]} ON {f.key[2]} TO {f.key[0]}"
        elif f.kind == "policy":
            what = f"row policy {f.key[0]} ON {f.key[1]}: {f.attrs[0]} -> {f.attrs[1]}"
            extra = extra if op.action != "add" else ""
        elif f.kind == "unrecognized":
            what = f"unrecognised grant on {f.key[0]}: {f.key[1]}"
        else:
            what = f"{f.kind} {f.key[0]}"
        return f"{sign} {what}{extra}"

    def directory_owned(self, fact: Fact) -> bool:
        """Facts about people follow the directory, so removing them does not need --prune."""
        if self.people is None:
            return False
        who = fact.key[0]
        return (fact.kind == "user" and fact.attrs == (LDAP,)) or (
            fact.kind in ("member", "grant") and (who in self._ldap_users or who in self.people)
        )

    def apply(self, plan: Plan, *, prune: bool = False, log=print) -> int:
        """Execute the plan. Removals need ``prune``. Returns the number of removals skipped."""
        skipped = 0
        for op in plan.ops:
            if op.action == "remove" and not prune and not self.directory_owned(op.fact):
                skipped += 1
                continue
            for sql in self.statements(op, reveal=True):
                log(f"  {_redact(sql)}")
                self.client.command(sql)
        return skipped


LIMIT_COLS = [
    "max_queries",
    "max_query_selects",
    "max_query_inserts",
    "max_errors",
    "max_result_rows",
    "max_result_bytes",
    "max_read_rows",
    "max_read_bytes",
    "max_execution_time",
    "max_written_bytes",
]


def _to_clause(sc: str) -> str:
    kind, _, names = sc.partition(":")
    listed = ", ".join(q(n) for n in names.split(",") if n)
    if kind == "all_except":
        return f"ALL EXCEPT {listed}" if listed else "ALL"
    return listed


def _settings_sql(rows: tuple) -> str:
    parts = []
    for name, value, mn, mx, writ in rows:
        s = f"{name} = {value}"
        if mn:
            s += f" MIN {mn}"
        if mx:
            s += f" MAX {mx}"
        if writ == "CONST":
            s += " CONST"
        parts.append(s)
    return ", ".join(parts)


def _remove_sql(f: Fact) -> list[str]:
    k = f.kind
    if k == "role":
        return [f"DROP ROLE IF EXISTS {q(f.key[0])}"]
    if k == "user":
        return [f"DROP USER IF EXISTS {q(f.key[0])}"]
    if k == "member":
        return [f"REVOKE {q(f.key[1])} FROM {q(f.key[0])}"]
    if k == "grant":
        who, priv, target = f.key
        return [f"REVOKE {priv} ON {target} FROM {q(who)}"]
    if k == "policy":
        return [f"DROP ROW POLICY IF EXISTS {q(f.key[0])} ON {f.key[1]}"]
    if k == "quota":
        return [f"DROP QUOTA IF EXISTS {q(f.key[0])}"]
    if k == "profile":
        return [f"DROP SETTINGS PROFILE IF EXISTS {q(f.key[0])}"]
    raise UnmanageableError(
        f"{f.key[1]!r} on {f.key[0]} is not something the model can express; revoke it by hand"
    )


def _redact(sql: str) -> str:
    return re.sub(r"(sha256_hash BY )'[^']*'", r"\1'<hidden>'", sql)


__all__ = ["KINDS", "ClickhouseAccess", "UnmanageableError", "desired_facts", "parse_show_grants"]
