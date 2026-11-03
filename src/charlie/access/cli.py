"""``charlie access``: plan, apply and drift for the access model."""

from __future__ import annotations

import sys
from pathlib import Path

from .. import ch
from . import clickhouse as chaccess
from . import directory, postgres
from .model import ModelError, load

ROOT = Path(__file__).resolve().parents[3]
MODEL = ROOT / "access" / "model.yaml"


def _engines(args, topo, model, secrets):
    out = []
    people = None
    if topo.ldap and not args.no_directory:
        people = directory.memberships(directory.connect(topo), topo.ldap.base_dn)
        unknown = sorted({r for rs in people.values() for r in rs} - set(model.role_names()))
        for r in unknown:
            print(
                f"warning: group {directory.PREFIX}{r} is not a role of the model", file=sys.stderr
            )
    if args.engine in ("clickhouse", "all"):
        out.append(chaccess.ClickhouseAccess(ch.first_alive(topo), model, secrets, people))
    if args.engine in ("postgres", "all") and topo.postgres:
        out.append(postgres.PostgresAccess(postgres.connect(topo), model, secrets, people))
    return out


def _lines(engine, plan, *, full: bool) -> list[str]:
    """One line per operation, except that a run of grants to the same role is folded."""
    if full:
        return [engine.describe(op) for op in plan.ops]
    out: list[str] = []
    runs: dict[tuple, list] = {}
    for op in plan.ops:
        if op.fact.kind == "grant" and op.action != "change":
            runs.setdefault((op.action, op.fact.key[0]), []).append(op)
    folded = {k for k, v in runs.items() if len(v) > 3}
    seen: set[tuple] = set()
    for op in plan.ops:
        k = (op.action, op.fact.key[0])
        if op.fact.kind == "grant" and k in folded:
            if k not in seen:
                seen.add(k)
                sign = {"add": "+", "remove": "-"}[op.action]
                first = engine.describe(runs[k][0]).split(" TO ")[0].lstrip("+- ")
                out.append(f"{sign} {len(runs[k])} grants to {op.fact.key[0]}  ({first}, ...)")
            continue
        out.append(engine.describe(op))
    return out


def _run(args, topo) -> int:
    model = load(args.model)
    try:
        secrets = model.secrets(strict=args.cmd == "apply")
    except ModelError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    exit_code = 0
    try:
        engines = _engines(args, topo, model, secrets)
    except directory.DirectoryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    for engine in engines:
        print(f"== {engine.name}")
        plan = engine.plan()
        if plan.empty:
            print("   in sync with the model")
            continue
        for line in _lines(engine, plan, full=args.full):
            print("  ", line)
        adds, changes, removes = (len(plan.of(a)) for a in ("add", "change", "remove"))
        print(f"   {adds} to add, {changes} to change, {removes} not in the model")
        if args.cmd == "plan":
            continue
        if args.cmd == "drift":
            exit_code = 1
            continue
        skipped = engine.apply(plan, prune=args.prune)
        if skipped:
            print(f"   {skipped} removals skipped: run with --prune to delete what the model lacks")
            exit_code = 1
    if args.cmd == "drift" and exit_code:
        print("\nconfigured access differs from access/model.yaml", file=sys.stderr)
    return exit_code


def register(sub) -> None:
    a = sub.add_parser("access", help="access model: plan, apply, drift")
    asub = a.add_subparsers(dest="access_cmd", required=True)
    for name, text in (
        ("plan", "show what would change"),
        ("apply", "make the cluster match the model"),
        ("drift", "exit 1 if the cluster differs from the model"),
    ):
        sp = asub.add_parser(name, help=text)
        sp.add_argument("--engine", choices=["clickhouse", "postgres", "all"], default="all")
        sp.add_argument("--model", default=str(MODEL))
        sp.add_argument("--full", action="store_true", help="list every grant, not a summary")
        sp.add_argument(
            "--no-directory",
            action="store_true",
            help="do not read people from LDAP; people already in the databases are left alone",
        )
        if name == "apply":
            sp.add_argument("--prune", action="store_true", help="also delete what the model lacks")
        sp.set_defaults(fn=lambda args, topo, n=name: _run(_with_cmd(args, n), topo))


def _directory(args, topo) -> int:
    model = load(args.model)
    conn = directory.connect(topo)
    base = topo.ldap.base_dn
    if args.dir_cmd == "seed":
        made = directory.seed(conn, base, model, Path(args.people))
        print("created: " + ", ".join(f"{n} {k}" for k, n in made.items()))
    elif args.dir_cmd == "show":
        for user, roles in sorted(directory.memberships(conn, base).items()):
            print(f"{user:<8} {', '.join(sorted(roles)) or '-'}")
    elif args.dir_cmd == "add":
        changed = directory.add_member(conn, base, args.user, args.role)
        print(
            f"{args.user} {'added to' if changed else 'already in'} {directory.PREFIX}{args.role}"
        )
    elif args.dir_cmd == "remove":
        changed = directory.remove_member(conn, base, args.user, args.role)
        verb = "removed from" if changed else "was not in"
        print(f"{args.user} {verb} {directory.PREFIX}{args.role}")
    return 0


def register_directory(sub) -> None:
    d = sub.add_parser("directory", help="the demo LDAP directory (stands in for the IdP)")
    dsub = d.add_subparsers(dest="dir_cmd", required=True)
    for name, text in (
        ("seed", "create groups and demo people"),
        ("show", "who is in which group"),
        ("add", "add a person to a group"),
        ("remove", "remove a person from a group (offboarding)"),
    ):
        sp = dsub.add_parser(name, help=text)
        sp.add_argument("--model", default=str(MODEL))
        if name == "seed":
            sp.add_argument(
                "--people", default=str(ROOT / "fixtures" / "directory" / "people.yaml")
            )
        if name in ("add", "remove"):
            sp.add_argument("user")
            sp.add_argument("role")
        sp.set_defaults(fn=_directory)


def _with_cmd(args, name):
    args.cmd = name
    return args


__all__ = ["register", "register_directory"]
