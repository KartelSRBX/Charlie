"""Dashboards as code: builds grafana/dashboards/*.json.

Run ``python grafana/build_dashboards.py`` after changing a panel. Grafana picks the JSON up
through file provisioning; nobody edits dashboards by hand in the UI. CI fails if the committed
JSON differs from what this script produces.
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).parent / "dashboards"
PROM = {"type": "prometheus", "uid": "prometheus"}

STAT = {
    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
    "colorMode": "value",
    "graphMode": "none",
    "textMode": "value",
    "justifyMode": "center",
}
LINE = {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 10, "showPoints": "never"}
TS_OPTS = {
    "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
    "tooltip": {"mode": "multi", "sort": "desc"},
}
GREEN, RED, ORANGE = "#73BF69", "#F2495C", "#FF9830"


def prom(expr: str, legend: str = "", ref: str = "A") -> dict:
    return {"datasource": PROM, "expr": expr, "legendFormat": legend, "refId": ref}


def steps(*pairs: tuple[str, float | None]) -> list[dict]:
    return [{"color": c, "value": v} for c, v in pairs]


class Board:
    def __init__(self, uid: str, title: str, refresh: str = "5s", time_from: str = "now-15m"):
        self.uid, self.title, self.refresh, self.time_from = uid, title, refresh, time_from
        self.panels: list[dict] = []
        self._id = 1
        self._x = self._y = self._row_h = 0

    def _place(self, w: int, h: int) -> dict:
        if self._x + w > 24:
            self._x, self._y, self._row_h = 0, self._y + self._row_h, 0
        pos = {"x": self._x, "y": self._y, "w": w, "h": h}
        self._x += w
        self._row_h = max(self._row_h, h)
        return pos

    def row(self, title: str) -> None:
        if self._x:
            self._x, self._y = 0, self._y + self._row_h
        self.panels.append(
            {
                "id": self._id,
                "type": "row",
                "title": title,
                "collapsed": False,
                "gridPos": {"x": 0, "y": self._y, "w": 24, "h": 1},
                "panels": [],
            }
        )
        self._id += 1
        self._y += 1
        self._row_h = 0

    def stat(self, title, expr, w=4, h=4, *, thresholds, unit=None, description="", decimals=0):
        self._add(
            "stat",
            title,
            [prom(expr)],
            w,
            h,
            description,
            unit=unit,
            decimals=decimals,
            thresholds=thresholds,
            options=STAT,
        )

    def series(self, title, targets, w=8, h=7, *, unit=None, description="", decimals=None):
        self._add(
            "timeseries",
            title,
            targets,
            w,
            h,
            description,
            unit=unit,
            decimals=decimals,
            custom=LINE,
            options=TS_OPTS,
        )

    def _add(self, kind, title, targets, w, h, description, *, unit, decimals, **extra):
        defaults: dict = {"custom": extra.get("custom", {})}
        if unit:
            defaults["unit"] = unit
        if decimals is not None:
            defaults["decimals"] = decimals
        if "thresholds" in extra:
            defaults["thresholds"] = {"mode": "absolute", "steps": extra["thresholds"]}
        defaults["color"] = {"mode": "thresholds" if kind == "stat" else "palette-classic"}
        self.panels.append(
            {
                "id": self._id,
                "type": kind,
                "title": title,
                "description": description,
                "datasource": PROM,
                "gridPos": self._place(w, h),
                "targets": targets,
                "fieldConfig": {"defaults": defaults, "overrides": []},
                "options": extra.get("options", {}),
            }
        )
        self._id += 1

    def json(self) -> dict:
        return {
            "uid": self.uid,
            "title": self.title,
            "tags": ["charlie"],
            "timezone": "browser",
            "schemaVersion": 39,
            "version": 1,
            "editable": True,
            "graphTooltip": 1,
            "refresh": self.refresh,
            "time": {"from": self.time_from, "to": "now"},
            "panels": self.panels,
            "templating": {"list": []},
            "annotations": {"list": []},
            "links": [],
        }


def cluster() -> Board:
    b = Board("charlie-cluster", "Charlie · Cluster health")

    b.row("Is the cluster healthy?")
    b.stat(
        "ClickHouse servers up",
        'count(up{job="clickhouse"} == 1) or vector(0)',
        thresholds=steps((RED, None), (ORANGE, 2), (GREEN, 4)),
        description="Of 4: a shard stays available while one replica of each shard is up.",
    )
    b.stat(
        "Keepers up",
        'count(up{job="keeper"} == 1) or vector(0)',
        thresholds=steps((RED, None), (ORANGE, 2), (GREEN, 3)),
        description="Replication and DDL need a majority: 2 of 3. With 1 left, writes stop.",
    )
    b.stat(
        "Keeper leaders",
        "sum(1 - ClickHouseAsyncMetrics_KeeperIsFollower) or vector(0)",
        thresholds=steps((RED, None), (GREEN, 1), (RED, 2)),
        description="Exactly one. Zero means an election is running or there is no quorum.",
    )
    b.stat(
        "Read-only replicas",
        "sum(ClickHouseMetrics_ReadonlyReplica) or vector(0)",
        thresholds=steps((GREEN, None), (RED, 1)),
        description="Replicas that lost their Keeper session and refuse writes.",
    )
    b.stat(
        "Max replica delay",
        "max(ClickHouseAsyncMetrics_ReplicasMaxAbsoluteDelay) or vector(0)",
        unit="s",
        thresholds=steps((GREEN, None), (ORANGE, 30), (RED, 300)),
        description="How far the slowest replica is behind its shard's newest data.",
    )
    b.stat(
        "Replication queue",
        "sum(ClickHouseAsyncMetrics_ReplicasSumQueueSize) or vector(0)",
        thresholds=steps((GREEN, None), (ORANGE, 100), (RED, 1000)),
        description="Entries waiting to be fetched or merged, summed over replicas.",
    )

    b.row("Writes and replication")
    b.series(
        "Rows inserted / s",
        [prom("rate(ClickHouseProfileEvents_InsertedRows[30s])", "{{node}}")],
        unit="short",
        description="Per server, on the server that stores the rows and on the one that "
        "received a Distributed insert and forwarded it.",
    )
    b.series(
        "Replication queue per replica",
        [prom("ClickHouseAsyncMetrics_ReplicasMaxQueueSize", "{{node}}")],
        description="Work this replica still has to do to match its shard.",
    )
    b.series(
        "Replica delay",
        [prom("ClickHouseAsyncMetrics_ReplicasMaxAbsoluteDelay", "{{node}}")],
        unit="s",
        description="Jumps while a replica is down or restarting, then falls back to zero.",
    )
    b.series(
        "Parts fetched from peers / s",
        [prom("rate(ClickHouseProfileEvents_ReplicatedPartFetches[30s])", "{{node}}")],
        description="A revived replica downloads the parts it missed from its sibling.",
    )
    b.series(
        "Distributed connection failures / s",
        [prom("rate(ClickHouseProfileEvents_DistributedConnectionFailTry[30s])", "{{node}}")],
        description="Attempts to reach a replica that did not answer; the query goes to another.",
    )
    b.series(
        "Files waiting to be forwarded",
        [prom("ClickHouseMetrics_DistributedFilesToInsert", "{{node}}")],
        description="Rows an async Distributed insert has queued for a shard that is not ready.",
    )

    b.row("Keeper ensemble")
    b.series(
        "Is leader",
        [prom("1 - ClickHouseAsyncMetrics_KeeperIsFollower", "{{node}}")],
        w=6,
        description="1 for the node that currently leads the Raft group.",
    )
    b.series(
        "Average request latency",
        [prom("ClickHouseAsyncMetrics_KeeperAvgLatency", "{{node}}")],
        w=6,
        unit="ms",
    )
    b.series(
        "Outstanding requests",
        [prom("ClickHouseMetrics_KeeperOutstandingRequests", "{{node}}")],
        w=6,
    )
    b.series(
        "Znodes",
        [prom("ClickHouseAsyncMetrics_KeeperZnodeCount", "{{node}}")],
        w=6,
        description="Replication metadata: part lists, queues and the DDL log.",
    )

    b.row("Servers")
    b.series(
        "Uptime",
        [prom("ClickHouseAsyncMetrics_Uptime", "{{node}}")],
        w=6,
        unit="s",
        description="Drops to zero on a restart.",
    )
    b.series(
        "Memory",
        [prom("ClickHouseMetrics_MemoryTracking", "{{node}}")],
        w=6,
        unit="bytes",
    )
    b.series(
        "Active data parts",
        [prom("ClickHouseMetrics_PartsActive", "{{node}}")],
        w=6,
    )
    b.series(
        "Queries / s",
        [prom("rate(ClickHouseProfileEvents_Query[30s])", "{{node}}")],
        w=6,
    )
    return b


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for board in (cluster(),):
        name = board.uid.removeprefix("charlie-")
        (OUT / f"{name}.json").write_text(json.dumps(board.json(), indent=2) + "\n")
        print(f"wrote {OUT / name}.json ({len(board.panels)} panels)")


if __name__ == "__main__":
    main()
