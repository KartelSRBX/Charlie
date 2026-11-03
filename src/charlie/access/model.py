"""The access model: a validated view of access/model.yaml, and the roles derived from it.

Everything that can be checked without a database is checked here, so a bad model fails in review
and in CI, never half-applied on a live cluster.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

IDENT = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Privileges whose name is stable in ClickHouse's SHOW GRANTS. Group privileges such as `CREATE`
# or `ALL` are rejected because the server prints them expanded, so they could never be compared
# with what the model says.
CH_PRIVILEGES = frozenset(
    {
        "SHOW",
        "SELECT",
        "INSERT",
        "ALTER",
        "CREATE TABLE",
        "CREATE VIEW",
        "DROP TABLE",
        "DROP VIEW",
        "TRUNCATE",
        "OPTIMIZE",
        "dictGet",
    }
)
PG_TABLE_PRIVILEGES = frozenset(
    {"SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}
)
PG_SCHEMA_PRIVILEGES = frozenset({"USAGE", "CREATE"})

QUOTA_LIMITS = frozenset(
    {
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
    }
)
UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800}


class ModelError(ValueError):
    """The access model is inconsistent."""


def _ident(value: str, what: str) -> str:
    if not IDENT.match(value):
        raise ValueError(f"{what} {value!r} must be lower_snake_case (letters, digits, _)")
    return value


class Level(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clickhouse: list[str]
    postgres: list[str]
    postgres_schema: list[str] = ["USAGE"]
    profile: str | None = None
    quota: str | None = None

    @field_validator("clickhouse")
    @classmethod
    def _ch(cls, v: list[str]) -> list[str]:
        bad = set(v) - CH_PRIVILEGES
        if bad:
            raise ValueError(
                f"unsupported ClickHouse privileges {sorted(bad)}; "
                f"use one of {sorted(CH_PRIVILEGES)}"
            )
        return v

    @field_validator("postgres")
    @classmethod
    def _pg(cls, v: list[str]) -> list[str]:
        bad = set(v) - PG_TABLE_PRIVILEGES
        if bad:
            raise ValueError(f"unsupported PostgreSQL table privileges {sorted(bad)}")
        return v

    @field_validator("postgres_schema")
    @classmethod
    def _pgs(cls, v: list[str]) -> list[str]:
        bad = set(v) - PG_SCHEMA_PRIVILEGES
        if bad:
            raise ValueError(f"unsupported PostgreSQL schema privileges {sorted(bad)}")
        return v


class ClickhouseDomain(BaseModel):
    model_config = ConfigDict(extra="forbid")
    databases: list[str]

    @field_validator("databases")
    @classmethod
    def _names(cls, v: list[str]) -> list[str]:
        return [_ident(x, "database") for x in v]


class PostgresDomain(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schemas: list[str]

    @field_validator("schemas")
    @classmethod
    def _names(cls, v: list[str]) -> list[str]:
        return [_ident(x, "schema") for x in v]


class Domain(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clickhouse: ClickhouseDomain
    postgres: PostgresDomain


class ScopedRole(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    base: str
    row_policies: dict[str, str] = Field(min_length=1)

    @field_validator("name", "base")
    @classmethod
    def _names(cls, v: str) -> str:
        return _ident(v, "role")

    @field_validator("row_policies")
    @classmethod
    def _filters(cls, v: dict[str, str]) -> dict[str, str]:
        for table, flt in v.items():
            db, _, tbl = table.partition(".")
            if not (IDENT.match(db) and IDENT.match(tbl)):
                raise ValueError(f"row policy target {table!r} must be database.table")
            if not flt.strip():
                raise ValueError(f"row policy filter for {table} is empty")
            if ";" in flt:
                raise ValueError(f"row policy filter for {table} must be a single expression")
        return v


class Setting(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int | float | str
    min: int | float | str | None = None
    max: int | float | str | None = None
    readonly: bool = False


class Quota(BaseModel):
    model_config = ConfigDict(extra="allow")
    interval: str

    @property
    def seconds(self) -> int:
        m = re.fullmatch(r"(\d+)\s*(second|minute|hour|day|week)s?", self.interval.strip())
        if not m:
            raise ValueError(f"interval {self.interval!r} must look like '1 day' or '30 minute'")
        return int(m[1]) * UNITS[m[2]]

    @property
    def limits(self) -> dict[str, int]:
        return {k: int(v) for k, v in (self.model_extra or {}).items()}

    @model_validator(mode="after")
    def _check(self) -> Quota:
        _ = self.seconds
        unknown = set(self.limits) - QUOTA_LIMITS
        if unknown:
            raise ValueError(f"unknown quota limits {sorted(unknown)}")
        if not self.limits:
            raise ValueError("a quota needs at least one limit")
        return self


class ServiceAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")
    roles: list[str]
    password_env: str

    @field_validator("password_env")
    @classmethod
    def _env(cls, v: str) -> str:
        if not ENV_NAME.match(v):
            raise ValueError(f"{v!r} is not an environment variable name")
        return v


class AccessModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int
    levels: dict[str, Level]
    domains: dict[str, Domain]
    scoped_roles: list[ScopedRole] = []
    profiles: dict[str, dict[str, Any]] = {}
    quotas: dict[str, Quota] = {}
    service_accounts: dict[str, ServiceAccount] = {}

    @model_validator(mode="after")
    def _consistent(self) -> AccessModel:
        if self.version != 1:
            raise ValueError(f"unsupported model version {self.version}")
        for n in [*self.levels, *self.domains, *self.profiles, *self.quotas]:
            _ident(n, "name")
        for lvl_name, lvl in self.levels.items():
            if lvl.profile and lvl.profile not in self.profiles:
                raise ValueError(f"level {lvl_name}: unknown profile {lvl.profile!r}")
            if lvl.quota and lvl.quota not in self.quotas:
                raise ValueError(f"level {lvl_name}: unknown quota {lvl.quota!r}")
        for name, settings in self.profiles.items():
            for sname, raw in settings.items():
                if not re.fullmatch(r"[a-z][a-z0-9_]*", sname):
                    raise ValueError(f"profile {name}: bad setting name {sname!r}")
                Setting.model_validate(raw if isinstance(raw, dict) else {"value": raw})

        names = [r.name for r in self.derived_roles()]
        dup = {n for n in names if names.count(n) > 1}
        if dup:
            raise ValueError(f"role names collide: {sorted(dup)}")
        known = set(names)

        for sr in self.scoped_roles:
            base = next((r for r in self.base_roles() if r.name == sr.base), None)
            if base is None:
                raise ValueError(f"scoped role {sr.name}: base {sr.base!r} is not a generated role")
            if not sr.name.startswith(f"{sr.base}_"):
                raise ValueError(
                    f"scoped role {sr.name!r} must start with its base role name '{sr.base}_'"
                )
            dbs = set(self.domains[base.domain].clickhouse.databases)
            for table in sr.row_policies:
                if table.partition(".")[0] not in dbs:
                    raise ValueError(
                        f"scoped role {sr.name}: {table} is outside the databases of the "
                        f"{base.domain} domain {sorted(dbs)}"
                    )
        for acct, spec in self.service_accounts.items():
            if not re.fullmatch(r"svc_[a-z0-9_]+", acct):
                raise ValueError(f"service account {acct!r} must be named svc_<purpose>")
            if acct in known:
                raise ValueError(f"service account {acct!r} collides with a role")
            for r in spec.roles:
                if r not in known:
                    raise ValueError(f"service account {acct}: unknown role {r!r}")
        return self

    # --- derived objects -------------------------------------------------------------------
    def base_roles(self) -> list[Role]:
        return [
            Role(name=f"{d}_{lvl}", domain=d, level=lvl)
            for d in self.domains
            for lvl in self.levels
        ]

    def derived_roles(self) -> list[Role]:
        base = {r.name: r for r in self.base_roles()}
        scoped = [
            Role(name=sr.name, domain=base[sr.base].domain, level=base[sr.base].level, scoped=True)
            for sr in self.scoped_roles
            if sr.base in base
        ]
        return [*base.values(), *scoped]

    def role_names(self) -> list[str]:
        return [r.name for r in self.derived_roles()]

    def secrets(self, environ: dict[str, str] | None = None, *, strict: bool = True) -> dict:
        """{service account: password} from the environment."""
        env = os.environ if environ is None else environ
        out, missing = {}, []
        for name, spec in self.service_accounts.items():
            if env.get(spec.password_env):
                out[name] = env[spec.password_env]
            else:
                missing.append(f"{name} ({spec.password_env})")
        if missing and strict:
            raise ModelError("missing passwords in the environment: " + ", ".join(missing))
        return out


@dataclass(frozen=True)
class Role:
    name: str
    domain: str
    level: str
    scoped: bool = False
    extra: dict = field(default_factory=dict, compare=False, hash=False)


def load(path: str | Path = "access/model.yaml") -> AccessModel:
    return AccessModel.model_validate(yaml.safe_load(Path(path).read_text()))
