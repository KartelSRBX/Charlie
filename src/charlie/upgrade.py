"""Rolling upgrade: move a running cluster to the images in topology.yaml, one server at a time.

The workflow is the one the rest of the repo uses: change ``images`` in topology.yaml, run
``charlie render`` (the diff in deploy/ is the review), then ``charlie upgrade``. Keepers go
first, followers before the leader, because ClickHouse servers must not be newer than the
Keeper ensemble they talk to; then the ClickHouse servers, one replica at a time. After each
step the next one waits until the server is healthy, connected to Keeper and has an empty
replication queue, so the shard always keeps one replica that has everything.
"""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import ch, status
from .loadgen import LoadGenerator
from .topology import Topology

COMPOSE = [
    "docker",
    "compose",
    "-f",
    str(Path(__file__).resolve().parents[2] / "deploy" / "docker-compose.yml"),
]


@dataclass
class Step:
    service: str
    before: str
    after: str
    seconds: float


@dataclass
class Report:
    steps: list[Step] = field(default_factory=list)
    versions_before: dict[str, str] = field(default_factory=dict)
    versions_after: dict[str, str] = field(default_factory=dict)
    rows_loaded: int = 0
    retries: int = 0
    failed_batches: int = 0
    reads: int = 0
    read_errors: int = 0
    seconds: float = 0.0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def targets(topo: Topology) -> dict[str, str]:
    out = {k.name: topo.images.keeper for k in topo.keeper_nodes}
    out |= {n.name: topo.images.server for n in topo.nodes}
    return out


def _compose(*args: str, timeout: int = 300) -> str:
    r = subprocess.run(
        [*COMPOSE, *args], capture_output=True, text=True, timeout=timeout, check=False
    )
    if r.returncode:
        raise RuntimeError(f"docker compose {' '.join(args)} failed: {r.stderr.strip()[-400:]}")
    return r.stdout.strip()


def running_image(service: str) -> str | None:
    cid = _compose("ps", "-q", service)
    if not cid:
        return None
    out = subprocess.run(
        ["docker", "inspect", "-f", "{{.Config.Image}}", cid], capture_output=True, text=True
    )
    return out.stdout.strip() or None


def server_versions(topo: Topology) -> dict[str, str]:
    out = {}
    for n in topo.nodes:
        try:
            out[n.name] = str(ch.connect(n.http_port, timeout=5).command("SELECT version()"))
        except Exception:
            out[n.name] = "down"
    return out


def order(topo: Topology) -> list[str]:
    """Keepers (followers first, leader last), then servers: replica 2 before replica 1."""
    states = {k.name: k.state for k in status.keeper_statuses(topo)}
    keepers = sorted((k.name for k in topo.keeper_nodes), key=lambda n: states.get(n) == "leader")
    nodes = sorted(topo.nodes, key=lambda n: (-n.replica, n.shard))
    return [*keepers, *(n.name for n in nodes)]


def _ready(topo: Topology, service: str) -> bool:
    ks = status.keeper_statuses(topo)
    if not (all(k.up for k in ks) and sum(k.state == "leader" for k in ks) == 1):
        return False
    node = next((n for n in topo.nodes if n.name == service), None)
    for st in status.node_statuses(topo):
        if node and st.name != service:
            continue
        if not (st.up and st.keeper_connected and st.readonly_tables == 0 and st.max_queue == 0):
            return False
    return True


def _wait(cond, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if cond():
                return
        except Exception:  # the server is restarting; ask again
            pass
        time.sleep(1)
    raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {what}")


def upgrade_one(topo: Topology, service: str, image: str) -> Step | None:
    before = running_image(service)
    if before == image:
        return None
    t0 = time.monotonic()
    _compose("up", "-d", "--no-deps", "--wait", service, timeout=300)
    _wait(lambda: _ready(topo, service), 180, f"{service} to be healthy and caught up")
    return Step(service, before or "-", image, time.monotonic() - t0)


def run(topo: Topology, under_load: bool = True, log=print) -> Report:
    rep = Report()
    rep.versions_before = server_versions(topo)
    want = targets(topo)
    todo = [s for s in order(topo) if running_image(s) != want[s]]
    log(f"{len(todo)} of {len(want)} servers need the new image")
    c = ch.first_alive(topo)
    before_rows = int(c.command("SELECT count() FROM igaming.bets"))
    t0 = time.monotonic()

    stop = threading.Event()
    gen = None
    threads: list[threading.Thread] = []
    if under_load:
        top = before_rows and int(c.command("SELECT max(bet_id) + 1 FROM igaming.bets"))
        gen = LoadGenerator(topo, batch_size=2000, retry_for=120, start=-(-top // 2000))
        threads.append(threading.Thread(target=lambda: gen.run(workers=4, until=stop)))

        def reader() -> None:
            # What an application sees: a query with failover across the nodes.
            while not stop.is_set():
                rep.reads += 1
                try:
                    ch.first_alive(topo).command("SELECT count() FROM igaming.bets")
                except Exception:
                    rep.read_errors += 1
                time.sleep(0.25)

        threads.append(threading.Thread(target=reader))
        for t in threads:
            t.start()
    try:
        for service in todo:
            step = upgrade_one(topo, service, want[service])
            if step:
                rep.steps.append(step)
                log(f"  {step.service}: {step.before} -> {step.after} in {step.seconds:.0f}s")
    finally:
        stop.set()
        for t in threads:
            t.join()
    rep.seconds = time.monotonic() - t0
    rep.versions_after = server_versions(topo)
    expected = before_rows
    if gen:
        rep.rows_loaded = gen.stats.rows_ok
        rep.retries = gen.stats.retries
        rep.failed_batches = len(gen.stats.failed_batches)
        expected += gen.stats.rows_ok
        if gen.stats.failed_batches:
            rep.problems.append(f"{len(gen.stats.failed_batches)} batches were never accepted")
    res = status.check_consistency(topo, expected_rows=expected)
    rep.problems += res.problems
    stale = [n for n, v in rep.versions_after.items() if v == "down"]
    if stale:
        rep.problems.append(f"servers not answering after the upgrade: {stale}")
    return rep
