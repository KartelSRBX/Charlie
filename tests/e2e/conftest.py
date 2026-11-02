"""Fixtures for tests that need the docker stack: ``make up migrate`` first, then ``make e2e``."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from charlie import ch, migrate
from charlie.cli import MIGRATIONS
from charlie.loadgen import LoadGenerator
from charlie.topology import load

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ["docker", "compose", "-f", str(ROOT / "deploy" / "docker-compose.yml")]


def compose(*args: str) -> None:
    subprocess.run([*COMPOSE, *args], check=True, capture_output=True, text=True, timeout=180)


def wait_for(cond, timeout: float = 90, what: str = "condition", every: float = 1.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            if cond():
                return
        except Exception as e:
            last = e
        time.sleep(every)
    raise TimeoutError(f"timed out after {timeout}s waiting for {what} (last error: {last})")


@pytest.fixture(scope="session")
def topo():
    return load(ROOT / "topology.yaml")


@pytest.fixture(scope="session", autouse=True)
def schema(topo):
    client = ch.first_alive(topo)
    migrate.ensure_meta(client)
    migrate.apply(client, migrate.plan(client, migrate.discover(MIGRATIONS)))


@pytest.fixture
def healthy(topo):
    """Start every container again after a test, even a failed one, and wait until it is whole."""
    yield
    compose("start")
    from charlie import status

    def whole():
        ks = status.keeper_statuses(topo)
        ns = status.node_statuses(topo)
        return (
            all(k.up for k in ks)
            and sum(k.state == "leader" for k in ks) == 1
            and all(n.up and n.keeper_connected and n.readonly_tables == 0 for n in ns)
        )

    wait_for(whole, timeout=120, what="the cluster to be whole again")


@pytest.fixture
def table_rows(topo):
    """Rows already in igaming.bets, so a test can assert on its own delta."""

    def count() -> int:
        return int(ch.first_alive(topo).command("SELECT count() FROM igaming.bets"))

    return count


@pytest.fixture
def make_gen(topo):
    def factory(**kw) -> LoadGenerator:
        c = ch.first_alive(topo)
        top = int(c.command("SELECT coalesce(max(bet_id) + 1, 0) FROM igaming.bets"))
        size = kw.pop("batch_size", 2000)
        return LoadGenerator(topo, batch_size=size, start=-(-top // size), **kw)

    return factory
