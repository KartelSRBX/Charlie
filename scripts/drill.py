"""A narrated failure drill for demos: keeps loading while things are killed and revived.

Open the Grafana dashboard (http://localhost:3000) and run ``make drill``. The sequence:
a replica dies, then a Keeper dies as well, then both come back. Writes never stop, and
at the end the script proves every acknowledged row exists exactly once.
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from charlie import ch, status
from charlie.loadgen import LoadGenerator
from charlie.topology import load

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ["docker", "compose", "-f", str(ROOT / "deploy" / "docker-compose.yml")]


def compose(*args: str) -> None:
    subprocess.run([*COMPOSE, *args], check=True, capture_output=True)


def say(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> int:
    topo = load(ROOT / "topology.yaml")
    client = ch.first_alive(topo)
    before = int(client.command("SELECT count() FROM igaming.bets"))
    top = int(client.command("SELECT coalesce(max(bet_id) + 1, 0) FROM igaming.bets"))
    gen = LoadGenerator(topo, batch_size=2000, attempts=8, start=-(-top // 2000))

    t = threading.Thread(target=lambda: gen.run(duration=130, workers=3))
    t.start()
    say("load started")
    time.sleep(20)
    say("killing replica ch-s1-r1")
    compose("stop", "ch-s1-r1")
    time.sleep(25)
    say("killing keeper-2 (2 of 3 keepers left, still a quorum)")
    compose("stop", "keeper-2")
    time.sleep(25)
    say("reviving keeper-2 and ch-s1-r1")
    compose("start", "keeper-2", "ch-s1-r1")
    t.join()
    say(
        f"load finished: {gen.stats.rows_ok} rows acknowledged, "
        f"{gen.stats.retries} retries, {len(gen.stats.failed_batches)} failed batches"
    )
    res = status.check_consistency(topo, expected_rows=before + gen.stats.rows_ok)
    for p in res.problems:
        say(f"PROBLEM: {p}")
    say(f"total={res.total} unique={res.unique} per node={res.per_node}")
    return 0 if res.ok and not gen.stats.failed_batches else 1


if __name__ == "__main__":
    raise SystemExit(main())
