"""The cluster topology: a validated model of topology.yaml and the node names derived from it."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Images(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server: str
    keeper: str


class Ports(BaseModel):
    model_config = ConfigDict(extra="forbid")
    http_base: int = 8123
    native_base: int = 9000
    keeper_base: int = 9181
    prometheus: int = 9090
    grafana: int = 3000


class Replica(BaseModel):
    """One ClickHouse server: shard 1, replica 2 is called ``ch-s1-r2``."""

    shard: int
    replica: int
    http_port: int
    native_port: int

    @property
    def name(self) -> str:
        return f"ch-s{self.shard}-r{self.replica}"

    @property
    def volume(self) -> str:
        return f"{self.name}-data"


class Keeper(BaseModel):
    id: int
    client_port: int

    @property
    def name(self) -> str:
        return f"keeper-{self.id}"

    @property
    def volume(self) -> str:
        return f"{self.name}-data"


class Topology(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cluster: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    images: Images
    keepers: int = Field(ge=1, le=7)
    shards: int = Field(ge=1, le=16)
    replicas: int = Field(ge=1, le=5)
    ports: Ports = Ports()

    @field_validator("keepers")
    @classmethod
    def _odd_quorum(cls, n: int) -> int:
        if n % 2 == 0:
            raise ValueError(
                f"keepers must be odd, got {n}: an even ensemble tolerates no more failures "
                f"than {n - 1} and can split into two equal halves"
            )
        return n

    @model_validator(mode="after")
    def _ports_do_not_collide(self) -> Topology:
        n = self.shards * self.replicas
        http = set(range(self.ports.http_base, self.ports.http_base + n))
        native = set(range(self.ports.native_base, self.ports.native_base + n))
        keeper = set(range(self.ports.keeper_base, self.ports.keeper_base + self.keepers))
        fixed = {self.ports.prometheus, self.ports.grafana}
        clashes = (
            (http & native)
            | (http & keeper)
            | (native & keeper)
            | ((http | native | keeper) & fixed)
        )
        if clashes:
            raise ValueError(f"host ports collide: {sorted(clashes)}")
        return self

    @property
    def nodes(self) -> list[Replica]:
        out = []
        for s in range(1, self.shards + 1):
            for r in range(1, self.replicas + 1):
                i = (s - 1) * self.replicas + (r - 1)
                out.append(
                    Replica(
                        shard=s,
                        replica=r,
                        http_port=self.ports.http_base + i,
                        native_port=self.ports.native_base + i,
                    )
                )
        return out

    @property
    def keeper_nodes(self) -> list[Keeper]:
        return [
            Keeper(id=i, client_port=self.ports.keeper_base + i - 1)
            for i in range(1, self.keepers + 1)
        ]

    def shard_nodes(self, shard: int) -> list[Replica]:
        return [n for n in self.nodes if n.shard == shard]

    @property
    def quorum(self) -> int:
        return self.keepers // 2 + 1


def load(path: str | Path = "topology.yaml") -> Topology:
    return Topology.model_validate(yaml.safe_load(Path(path).read_text()))
