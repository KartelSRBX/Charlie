from pathlib import Path

import pytest

from charlie.access import clickhouse as chx
from charlie.access import postgres as pgx
from charlie.access.facts import Fact, diff
from charlie.access.model import load

ROOT = Path(__file__).resolve().parents[1]
ORDER = ("role", "member", "grant")


@pytest.fixture(scope="module")
def model():
    return load(ROOT / "access" / "model.yaml")


def fake_norm(table: str, expr: str) -> str:
    return expr


# --- diff -----------------------------------------------------------------------------------
def test_diff_of_equal_sets_is_empty():
    s = {Fact("role", ("a",)), Fact("grant", ("a", "SELECT", "db.*"))}
    assert diff(s, set(s), ORDER).empty


def test_diff_orders_additions_by_dependency_and_removals_the_other_way():
    want = {Fact("role", ("r",)), Fact("grant", ("r", "SELECT", "db.*"))}
    plan = diff(want, set(), ORDER)
    assert [o.fact.kind for o in plan.ops] == ["role", "grant"]

    have = {Fact("role", ("r",)), Fact("member", ("u", "r")), Fact("grant", ("r", "SELECT", "x"))}
    plan = diff(set(), have, ORDER)
    assert [o.fact.kind for o in plan.ops] == ["grant", "member", "role"]


def test_diff_same_identity_with_other_attrs_is_a_change_not_add_plus_remove():
    plan = diff({Fact("user", ("u",), ("ldap",))}, {Fact("user", ("u",), ("plaintext",))}, ORDER)
    assert [o.action for o in plan.ops] == ["change"]
    assert plan.ops[0].before.attrs == ("plaintext",)


def test_diff_never_removes_before_it_has_added():
    want = {Fact("grant", ("new", "SELECT", "db.*"))}
    have = {Fact("grant", ("old", "SELECT", "db.*"))}
    assert [o.action for o in diff(want, have, ORDER).ops] == ["add", "remove"]


# --- ClickHouse -----------------------------------------------------------------------------
def test_parse_show_grants_splits_privileges_and_roles():
    facts = chx.parse_show_grants(
        "r",
        [
            "GRANT SELECT, INSERT ON igaming.* TO r",
            "GRANT parent TO r",
            "GRANT SELECT ON a.b TO r WITH GRANT OPTION",
            "GRANT parent2 TO r WITH ADMIN OPTION",
            "something unexpected",
        ],
    )
    assert Fact("grant", ("r", "SELECT", "igaming.*")) in facts
    assert Fact("grant", ("r", "INSERT", "igaming.*")) in facts
    assert Fact("member", ("r", "parent")) in facts
    kinds = [f.kind for f in facts if f.kind == "unrecognized"]
    assert len(kinds) == 3  # grant option, admin option and the unparsable line


def test_parse_show_grants_unquotes_backticked_names():
    facts = chx.parse_show_grants("r", ["GRANT `igaming_ro` TO `r`"])
    assert Fact("member", ("r", "igaming_ro")) in facts


def test_clickhouse_desired_has_a_role_and_whitelisted_grants_per_domain_and_level(model):
    facts = chx.desired_facts(model, fake_norm)
    assert Fact("role", ("finance_rw",)) in facts
    assert Fact("grant", ("finance_rw", "INSERT", "finance.*")) in facts
    assert Fact("grant", ("finance_ro", "INSERT", "finance.*")) not in facts
    # a domain role never reaches another domain's database
    assert not any(
        f.kind == "grant" and f.key[0] == "igaming_ro" and "finance" in f.key[2] for f in facts
    )


def test_scoped_roles_get_their_base_privileges_but_only_their_rows(model):
    facts = chx.desired_facts(model, fake_norm)
    assert Fact("grant", ("igaming_ro_aurora", "SELECT", "igaming.*")) in facts
    pol = {f.key: f.attrs for f in facts if f.kind == "policy"}
    assert pol[("igaming_ro_aurora__bets_local", "igaming.bets_local")] == (
        "brand = 'aurora'",
        "to:igaming_ro_aurora",
    )


def test_a_restricted_table_also_gets_an_allow_all_policy_for_everybody_else(model):
    facts = chx.desired_facts(model, fake_norm)
    allow = next(f for f in facts if f.kind == "policy" and f.key[0].startswith("allow_all__"))
    assert allow.key[1] == "igaming.bets_local"
    assert allow.attrs[1] == "all_except:igaming_ro_aurora,igaming_ro_bluefin"


def test_the_server_normalises_filters_so_the_diff_compares_like_with_like(model):
    seen = []

    def spy(table, expr):
        seen.append((table, expr))
        return expr.upper()

    facts = chx.desired_facts(model, spy)
    assert ("igaming.bets_local", "brand = 'aurora'") in seen
    assert any(f.kind == "policy" and f.attrs[0] == "BRAND = 'AURORA'" for f in facts)


def test_quota_and_profile_are_attached_to_every_role_of_the_level(model):
    facts = chx.desired_facts(model, fake_norm)
    quota = next(f for f in facts if f.kind == "quota")
    assert quota.attrs[0] == 86400
    assert quota.attrs[2].startswith("to:") and "igaming_ro_aurora" in quota.attrs[2]
    profile = next(f for f in facts if f.key == ("analyst",))
    assert ("max_memory_usage", "4000000000", "", "8000000000", "CONST") in profile.attrs[0]


def test_people_become_ldap_users_with_the_roles_the_directory_gives_them(model):
    people = {"bob": {"igaming_ro", "not_a_role"}, "nobody": set()}
    facts = chx.desired_facts(model, fake_norm, people)
    assert Fact("user", ("bob",), ("ldap",)) in facts
    assert Fact("member", ("bob", "igaming_ro")) in facts
    assert not any(f.key[0] == "nobody" for f in facts)
    assert not any(f.kind == "member" and f.key[1] == "not_a_role" for f in facts)


def test_without_a_directory_no_person_is_expected(model):
    facts = chx.desired_facts(model, fake_norm)
    assert not any(f.attrs == ("ldap",) for f in facts if f.kind == "user")


class _NoClient:
    pass


def test_passwords_never_appear_in_logged_sql(model):
    acc = chx.ClickhouseAccess(_NoClient(), model, {"svc_loader": "s3cret"})
    op = next(
        o
        for o in diff(chx.desired_facts(model, fake_norm), set(), chx.KINDS).ops
        if o.fact.key == ("svc_loader",) and o.fact.kind == "user"
    )
    shown = acc.statements(op)[0]
    real = acc.statements(op, reveal=True)[0]
    assert "<hidden>" in shown
    assert "s3cret" not in real  # only its hash is sent
    assert chx.sha256_hex("s3cret") in real
    assert chx._redact(real) == shown


def test_removal_sql_covers_every_kind(model):
    assert chx._remove_sql(Fact("member", ("u", "r"))) == ["REVOKE `r` FROM `u`"]
    assert chx._remove_sql(Fact("grant", ("r", "SELECT", "db.*"))) == [
        "REVOKE SELECT ON db.* FROM `r`"
    ]
    assert chx._remove_sql(Fact("policy", ("p", "db.t"))) == [
        "DROP ROW POLICY IF EXISTS `p` ON db.t"
    ]
    with pytest.raises(chx.UnmanageableError):
        chx._remove_sql(Fact("unrecognized", ("r", "GRANT x ON y TO r WITH GRANT OPTION")))


def test_identifiers_cannot_break_out_of_their_quotes():
    assert chx.q("a`; DROP USER admin; --") == "`a; DROP USER admin; --`"


# --- PostgreSQL -----------------------------------------------------------------------------
CATALOG = pgx.Catalog(
    tables={"igaming": ["players"], "finance": ["ledger"]},
    sequences={"igaming": [], "finance": ["ledger_entry_id_seq"]},
)


def test_postgres_roles_are_nologin_groups_and_service_accounts_use_scram(model):
    facts = pgx.desired_facts(model, CATALOG)
    assert Fact("role", ("igaming_ro",), (pgx.NOLOGIN, "group")) in facts
    assert Fact("role", ("svc_loader",), (pgx.SCRAM, "service")) in facts
    assert Fact("member", ("svc_loader", "igaming_rw")) in facts
    # row-scoped roles exist in ClickHouse only
    assert not any(f.key[0] == "igaming_ro_aurora" for f in facts)


def test_postgres_grants_cover_existing_objects_and_future_ones(model):
    facts = pgx.desired_facts(model, CATALOG)
    assert Fact("grant", ("finance_ro", "SELECT", "table", "finance.ledger")) in facts
    assert Fact("grant", ("finance_ro", "SELECT", "default_table", "finance")) in facts
    assert Fact("grant", ("finance_ro", "USAGE", "schema", "finance")) in facts
    assert Fact("grant", ("finance_ro", "CREATE", "schema", "finance")) not in facts
    assert Fact("grant", ("finance_admin", "CREATE", "schema", "finance")) in facts


def test_only_roles_that_can_insert_get_the_sequences(model):
    facts = pgx.desired_facts(model, CATALOG)
    seq = Fact("grant", ("finance_rw", "USAGE", "sequence", "finance.ledger_entry_id_seq"))
    assert seq in facts
    assert not any(
        f.kind == "grant" and f.key[0] == "finance_ro" and f.key[2] == "sequence" for f in facts
    )


def test_postgres_people_get_a_passwordless_login_role_and_memberships(model):
    people = {"bob": {"igaming_ro", "igaming_ro_aurora"}, "alice": {"igaming_ro_aurora"}}
    facts = pgx.desired_facts(model, CATALOG, people)
    assert Fact("role", ("bob",), (pgx.EXTERNAL, "person")) in facts
    assert Fact("member", ("bob", "igaming_ro")) in facts
    assert not any(f.kind == "member" and f.key[1] == "igaming_ro_aurora" for f in facts)
    # alice only holds a ClickHouse-only role: nothing to create in PostgreSQL
    assert not any(f.key[0] == "alice" for f in facts)


def test_postgres_sql_for_each_grant_kind():
    g = pgx._grant_sql
    assert g("GRANT", "TO", "r", "SELECT", "table", "s.t") == 'GRANT SELECT ON TABLE "s"."t" TO "r"'
    assert g("REVOKE", "FROM", "PUBLIC", "USAGE", "schema", "s") == (
        'REVOKE USAGE ON SCHEMA "s" FROM PUBLIC'
    )
    assert g("GRANT", "TO", "r", "SELECT", "default_table", "s") == (
        'ALTER DEFAULT PRIVILEGES IN SCHEMA "s" GRANT SELECT ON TABLES TO "r"'
    )


def test_postgres_quoting_resists_injection():
    assert pgx.qi('a"b') == '"a""b"'
    assert pgx.ql("it's") == "'it''s'"
    assert pgx.qi("PUBLIC") == "PUBLIC"


def test_dropping_a_role_first_ends_its_sessions():
    sqls = pgx._remove_sql(Fact("role", ("bob",), (pgx.EXTERNAL, "person")))
    assert "pg_terminate_backend" in sqls[0] and sqls[-1] == 'DROP ROLE IF EXISTS "bob"'


def test_postgres_password_never_appears_in_logs():
    assert pgx._redact("CREATE ROLE x LOGIN PASSWORD 'SCRAM-SHA-256$4096:abc'") == (
        "CREATE ROLE x LOGIN PASSWORD '<hidden>'"
    )
