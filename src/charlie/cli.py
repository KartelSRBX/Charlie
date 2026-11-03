"""``charlie``: render the deployment, migrate the schema, inspect and exercise the cluster."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import ch, migrate, render, status
from .access import cli as access_cli
from .loadgen import LoadGenerator
from .topology import load

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
MIGRATIONS = ROOT / "schema" / "migrations"


def cmd_render(args, topo) -> int:
    changed = render.write(topo, DEPLOY)
    for rel in changed:
        print(f"wrote deploy/{rel}")
    print(f"{len(render.render(topo))} files, {len(changed)} changed")
    return 0


def cmd_check(args, topo) -> int:
    bad = render.drift(topo, DEPLOY)
    for item in bad:
        print(f"drift: deploy/{item}")
    if bad:
        print("deploy/ does not match topology.yaml; run `charlie render`", file=sys.stderr)
        return 1
    print("deploy/ matches topology.yaml")
    return 0


def cmd_migrate(args, topo) -> int:
    migrations = migrate.discover(MIGRATIONS)
    client = ch.first_alive(topo)
    migrate.ensure_meta(client)
    pending = migrate.plan(client, migrations)
    if not pending:
        print("schema is up to date")
        return 0
    if args.plan:
        for m in pending:
            print(f"pending: {m.version:03d}_{m.name} ({len(m.statements)} statements)")
        return 0
    migrate.apply(client, pending)
    return 0


def cmd_status(args, topo) -> int:
    ok = True
    keepers = status.keeper_statuses(topo)
    print(f"keeper ensemble ({topo.keepers} nodes, quorum {topo.quorum})")
    for k in keepers:
        extra = f"followers={k.followers}" if k.state == "leader" else ""
        print(f"  {k.name:<10} {'up  ' if k.up else 'DOWN'} {k.state:<9} znodes={k.znodes} {extra}")
    alive = sum(k.up for k in keepers)
    ok &= alive >= topo.quorum
    print(f"clickhouse ({topo.shards} shards x {topo.replicas} replicas)")
    for n in status.node_statuses(topo):
        if not n.up:
            print(f"  {n.name:<9} DOWN ({n.error})")
            ok = False
            continue
        print(
            f"  {n.name:<9} up   keeper={'ok' if n.keeper_connected else 'LOST'} "
            f"readonly_tables={n.readonly_tables} queue={n.max_queue} "
            f"delay={n.max_delay_s}s rows={n.rows}"
        )
        ok &= n.keeper_connected and n.readonly_tables == 0
    return 0 if ok else 1


def cmd_load(args, topo) -> int:
    client = ch.first_alive(topo)
    start = 0
    if int(client.command("EXISTS TABLE igaming.bets")):
        top = int(client.command("SELECT coalesce(max(bet_id) + 1, 0) FROM igaming.bets"))
        start = -(-top // args.batch_size)  # continue after the last batch, no id overlap
    gen = LoadGenerator(topo, batch_size=args.batch_size, start=start)
    stats = gen.run(batches=args.batches, duration=args.duration, workers=args.workers)
    print(
        f"sent {stats.rows_ok} rows in {stats.batches_ok} batches "
        f"({stats.rows_per_s:,.0f} rows/s, {stats.retries} retries, "
        f"{len(stats.failed_batches)} failed batches)"
    )
    return 0 if not stats.failed_batches else 1


def cmd_verify(args, topo) -> int:
    res = status.check_consistency(topo, expected_rows=args.expect)
    print(f"total={res.total} unique={res.unique}")
    for name, rows in res.per_node.items():
        print(f"  {name:<9} {rows}")
    for p in res.problems:
        print(f"PROBLEM: {p}")
    return 0 if res.ok else 1


def cmd_backup(args, topo) -> int:
    from . import backup

    c = backup.admin(topo)
    if args.backup_cmd == "create":
        name = args.name or time.strftime(
            "%s-%%Y%%m%%d-%%H%%M%%S" % ("incr" if args.base else "full")
        )
        r = backup.create(c, topo, name, base=args.base)
        print(
            f"{r.name}: {r.stored_bytes:,} bytes in {r.files} files, {r.seconds:.1f}s"
            + (f" (increment on {r.base})" if r.base else "")
        )
    elif args.backup_cmd == "list":
        for name, size, files in backup.list_backups(c, topo):
            print(f"{name:<24} {size:>14,} bytes  {files:>4} files")
    elif args.backup_cmd == "restore":
        print(f"restored {args.name} in {backup.restore(c, topo, args.name):.1f}s")
    elif args.backup_cmd == "drill":
        rep = backup.drill(topo, rows=args.rows)
        for p in rep.problems:
            print(f"PROBLEM: {p}")
        print("restore drill:", "OK" if rep.ok else "FAILED")
        return 0 if rep.ok else 1
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="charlie", description=__doc__)
    p.add_argument("--topology", default=str(ROOT / "topology.yaml"))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("render", help="write deploy/ from topology.yaml").set_defaults(fn=cmd_render)
    sub.add_parser("check", help="fail if deploy/ drifted from topology.yaml").set_defaults(
        fn=cmd_check
    )
    m = sub.add_parser("migrate", help="apply pending schema migrations on the cluster")
    m.add_argument("--plan", action="store_true", help="only list what would run")
    m.set_defaults(fn=cmd_migrate)
    sub.add_parser("status", help="keeper and replica health").set_defaults(fn=cmd_status)
    ld = sub.add_parser("load", help="insert generated bets")
    ld.add_argument("--batches", type=int, default=None)
    ld.add_argument("--duration", type=float, default=None, help="seconds")
    ld.add_argument("--batch-size", type=int, default=5000)
    ld.add_argument("--workers", type=int, default=4)
    ld.set_defaults(fn=cmd_load)
    v = sub.add_parser("verify", help="check totals, duplicates, replica equality, shard balance")
    v.add_argument("--expect", type=int, default=None, help="exact number of rows expected")
    v.set_defaults(fn=cmd_verify)

    b = sub.add_parser("backup", help="backups to object storage and the restore drill")
    bsub = b.add_subparsers(dest="backup_cmd", required=True)
    bc = bsub.add_parser("create", help="back up all databases (full, or an increment with --base)")
    bc.add_argument("--name")
    bc.add_argument("--base", help="name of the backup this one builds on")
    bsub.add_parser("list", help="backups in the bucket")
    br = bsub.add_parser("restore", help="restore a backup into a cluster that lacks the databases")
    br.add_argument("name")
    bd = bsub.add_parser("drill", help="back up, destroy, restore, compare (destructive!)")
    bd.add_argument("--rows", type=int, default=200_000)
    b.set_defaults(fn=cmd_backup)

    access_cli.register(sub)
    access_cli.register_directory(sub)

    args = p.parse_args(argv)
    if args.cmd == "load" and args.batches is None and args.duration is None:
        p.error("load needs --batches or --duration")
    return args.fn(args, load(args.topology))


if __name__ == "__main__":
    raise SystemExit(main())
