from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from charlie.access.model import AccessModel, ModelError, load

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def raw() -> dict:
    return yaml.safe_load((ROOT / "access" / "model.yaml").read_text())


def build(raw: dict) -> AccessModel:
    return AccessModel.model_validate(raw)


def test_repo_model_loads_and_generates_domain_level_roles():
    m = load(ROOT / "access" / "model.yaml")
    base = {r.name for r in m.base_roles()}
    assert base == {f"{d}_{lvl}" for d in ("igaming", "finance") for lvl in ("ro", "rw", "admin")}
    assert {"igaming_ro_aurora", "igaming_ro_bluefin"} <= set(m.role_names())


def test_scoped_role_inherits_domain_and_level_from_its_base():
    m = load(ROOT / "access" / "model.yaml")
    scoped = next(r for r in m.derived_roles() if r.name == "igaming_ro_aurora")
    assert (scoped.domain, scoped.level, scoped.scoped) == ("igaming", "ro", True)


def test_group_privileges_are_rejected_because_the_server_prints_them_expanded(raw):
    raw["levels"]["ro"]["clickhouse"] = ["ALL"]
    with pytest.raises(ValidationError, match="unsupported ClickHouse privileges"):
        build(raw)


def test_unknown_postgres_privilege_is_rejected(raw):
    raw["levels"]["rw"]["postgres"] = ["SELECT", "SUPERUSER"]
    with pytest.raises(ValidationError, match="unsupported PostgreSQL"):
        build(raw)


def test_level_cannot_point_at_a_missing_profile(raw):
    raw["levels"]["ro"]["profile"] = "ghost"
    with pytest.raises(ValidationError, match="unknown profile"):
        build(raw)


def test_scoped_role_must_be_named_after_its_base(raw):
    raw["scoped_roles"][0]["name"] = "aurora_only"
    with pytest.raises(ValidationError, match="must start with its base role name"):
        build(raw)


def test_scoped_role_cannot_reach_outside_its_domain(raw):
    raw["scoped_roles"][0]["row_policies"] = {"finance.ledger_local": "1"}
    with pytest.raises(ValidationError, match="outside the databases"):
        build(raw)


def test_scoped_role_needs_a_generated_base(raw):
    raw["scoped_roles"][0]["base"] = "igaming_superuser"
    with pytest.raises(ValidationError, match="not a generated role"):
        build(raw)


def test_filter_must_be_a_single_expression(raw):
    raw["scoped_roles"][0]["row_policies"] = {"igaming.bets_local": "1; DROP TABLE x"}
    with pytest.raises(ValidationError, match="single expression"):
        build(raw)


def test_service_account_names_roles_and_env_var(raw):
    raw["service_accounts"]["svc_x"] = {"roles": ["nope"], "password_env": "X_PASSWORD"}
    with pytest.raises(ValidationError, match="unknown role"):
        build(raw)
    raw["service_accounts"]["svc_x"] = {"roles": ["igaming_ro"], "password_env": "not valid"}
    with pytest.raises(ValidationError, match="environment variable"):
        build(raw)
    raw["service_accounts"]["loader"] = {"roles": ["igaming_ro"], "password_env": "X"}
    with pytest.raises(ValidationError, match="svc_"):
        build(raw)


def test_a_service_account_cannot_shadow_a_role(raw):
    raw["service_accounts"]["svc_igaming_ro"] = {"roles": ["igaming_ro"], "password_env": "X"}
    build(raw)  # distinct name is fine
    raw["domains"]["svc"] = raw["domains"]["igaming"]
    raw["levels"] = {"ro": raw["levels"]["ro"]}
    raw["scoped_roles"] = []
    raw["service_accounts"] = {"svc_ro": {"roles": ["svc_ro"], "password_env": "X"}}
    with pytest.raises(ValidationError, match="collides with a role"):
        build(raw)


def test_quota_needs_a_valid_interval_and_known_limits(raw):
    raw["quotas"]["analyst_daily"]["interval"] = "forever"
    with pytest.raises(ValidationError, match="interval"):
        build(raw)
    raw["quotas"]["analyst_daily"] = {"interval": "1 day", "max_dreams": 3}
    with pytest.raises(ValidationError, match="unknown quota limits"):
        build(raw)


def test_quota_interval_in_seconds(raw):
    m = build(raw)
    assert m.quotas["analyst_daily"].seconds == 86400


def test_role_names_must_be_snake_case(raw):
    raw["domains"]["Bad-Domain"] = raw["domains"]["igaming"]
    with pytest.raises(ValidationError, match="lower_snake_case"):
        build(raw)


def test_secrets_come_from_the_environment_and_missing_ones_are_reported(raw):
    m = build(raw)
    assert m.secrets({"SVC_LOADER_PASSWORD": "a", "SVC_REPORTING_PASSWORD": "b"}) == {
        "svc_loader": "a",
        "svc_reporting": "b",
    }
    with pytest.raises(ModelError, match=r"svc_reporting \(SVC_REPORTING_PASSWORD\)"):
        m.secrets({"SVC_LOADER_PASSWORD": "a"})
    assert m.secrets({}, strict=False) == {}


def test_the_model_file_holds_no_password(raw):
    text = (ROOT / "access" / "model.yaml").read_text().lower()
    assert "password:" not in text
