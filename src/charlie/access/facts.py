"""Engine-neutral comparison of desired and observed access state.

Both sides are reduced to a set of ``Fact`` values: a *kind* (role, grant, row policy, ...), a
*key* that identifies the object and *attrs* that describe it. Two facts with the same kind and
key but different attrs are a change; a fact only on the observed side is something nobody wrote
down, which is drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple


class Fact(NamedTuple):
    kind: str
    key: tuple
    attrs: tuple = ()

    @property
    def ident(self) -> tuple:
        return (self.kind, self.key)


@dataclass(frozen=True)
class Op:
    action: str  # "add" | "change" | "remove"
    fact: Fact
    before: Fact | None = None  # for "change": what is there now


@dataclass
class Plan:
    ops: list[Op]
    kind_order: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not self.ops

    def of(self, action: str) -> list[Op]:
        return [o for o in self.ops if o.action == action]


def diff(desired: set[Fact], observed: set[Fact], kind_order: tuple[str, ...]) -> Plan:
    """Operations that turn ``observed`` into ``desired``, in an order that is safe to execute.

    Order: additions create dependencies first (roles before grants), removals go the other way
    round (grants before the role they are on), and changes are applied where they are.
    """
    want = {f.ident: f for f in desired}
    have = {f.ident: f for f in observed}
    rank = {k: i for i, k in enumerate(kind_order)}

    def sort_key(f: Fact) -> tuple:
        return (rank.get(f.kind, len(rank)), tuple(map(str, f.key)))

    ops = []
    for ident in sorted(want, key=lambda i: sort_key(want[i])):
        if ident not in have:
            ops.append(Op("add", want[ident]))
        elif want[ident] != have[ident]:
            ops.append(Op("change", want[ident], have[ident]))
    removals = [have[i] for i in have if i not in want]
    for f in sorted(removals, key=sort_key, reverse=True):
        ops.append(Op("remove", f))
    # additions and changes first, then removals: never leave a gap in access while swapping.
    order = {"add": 0, "change": 1, "remove": 2}
    ops.sort(key=lambda o: order[o.action])
    return Plan(ops, kind_order)
