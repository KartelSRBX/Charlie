"""A small, retry-safe load generator for igaming.bets.

Batch ``i`` always contains the same rows (its random seed is ``i``), so when a request fails
and is retried, possibly on another node, ClickHouse sees an identical block and deduplicates it
(``replicated_deduplication_window``). That is what lets the chaos tests assert exact counts:
every acknowledged row is present exactly once, whatever was killed in the meantime.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from clickhouse_connect.driver.client import Client

from . import ch
from .topology import Topology

BRANDS = ["aurora", "bluefin", "cobalt", "dune"]
GAMES = ["book_of_ra", "gates_olympus", "sweet_bonanza", "blackjack", "roulette", "crash", "dice"]
CURRENCIES = ["EUR", "USD", "RSD"]
COLUMNS = ["bet_id", "ts", "player_id", "brand", "game", "currency", "stake", "payout"]
PLAYERS = 50_000


def make_batch(index: int, size: int, ts: datetime) -> list[list]:
    """Rows for batch ``index``. Pure: the same arguments always give the same rows."""
    rng = random.Random(index)
    rows = []
    for i in range(size):
        stake = Decimal(rng.choice([1, 2, 5, 10, 20, 50])) + Decimal(rng.randint(0, 99)) / 100
        won = rng.random() < 0.46
        payout = (stake * Decimal(rng.choice([2, 2, 3, 5, 10]))).quantize(Decimal("0.01"))
        rows.append(
            [
                index * size + i,
                ts + timedelta(milliseconds=i),
                rng.randrange(PLAYERS),
                rng.choice(BRANDS),
                rng.choice(GAMES),
                rng.choice(CURRENCIES),
                stake,
                payout if won else Decimal("0.00"),
            ]
        )
    return rows


@dataclass
class Stats:
    batches_ok: int = 0
    rows_ok: int = 0
    retries: int = 0
    failed_batches: list[int] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)

    @property
    def rows_per_s(self) -> float:
        return self.rows_ok / max(time.monotonic() - self.started, 1e-9)


class LoadGenerator:
    """Inserts batches into ``igaming.bets`` through any live node, failing over on errors."""

    def __init__(
        self, topo: Topology, batch_size: int = 5000, retry_for: float = 60.0, start: int = 0
    ):
        self.topo = topo
        self.batch_size = batch_size
        # A real client retries for a bounded time, not a fixed number of tries: how long a
        # node takes to come back varies, the patience of a caller does not.
        self.retry_for = retry_for
        self.stats = Stats()
        self._lock = threading.Lock()
        self.start = start
        self._next = start
        self._clients: dict[int, Client] = {}
        self._ts0 = datetime.now(UTC).replace(tzinfo=None)

    def _client(self, port: int) -> Client:
        c = self._clients.get(port)
        if c is None:
            # Synchronous Distributed insert: the HTTP 200 means the rows reached the shards.
            c = ch.connect(port, timeout=30, insert_distributed_sync=1)
            self._clients[port] = c
        return c

    def next_index(self) -> int:
        with self._lock:
            i = self._next
            self._next += 1
            return i

    def send(self, index: int) -> bool:
        ts = self._ts0 + timedelta(seconds=index)
        rows = make_batch(index, self.batch_size, ts)
        eps = ch.endpoints(self.topo)
        give_up = time.monotonic() + self.retry_for
        attempt = -1
        while True:
            attempt += 1
            ep = eps[(index + attempt) % len(eps)]
            try:
                self._client(ep.port).insert("igaming.bets", rows, column_names=COLUMNS)
            except Exception:
                self._clients.pop(ep.port, None)
                if time.monotonic() >= give_up:
                    with self._lock:
                        self.stats.failed_batches.append(index)
                    return False
                with self._lock:
                    self.stats.retries += 1
                time.sleep(min(0.2 * 2**attempt, 2.0))
                continue
            with self._lock:
                self.stats.batches_ok += 1
                self.stats.rows_ok += len(rows)
            return True

    def run(
        self,
        batches: int | None = None,
        duration: float | None = None,
        workers: int = 4,
        until: threading.Event | None = None,
    ):
        """Send ``batches`` batches, run for ``duration`` seconds, or until ``until`` is set."""
        deadline = time.monotonic() + duration if duration else None

        def worker() -> None:
            while True:
                if (deadline and time.monotonic() >= deadline) or (until and until.is_set()):
                    return
                i = self.next_index()
                if batches is not None and i >= self.start + batches:
                    return
                self.send(i)

        threads = [threading.Thread(target=worker) for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return self.stats
