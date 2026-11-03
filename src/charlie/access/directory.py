"""The directory (LDAP) that stands in for an identity provider.

People and their group membership belong to the IdP. This module only reads them, so that
PostgreSQL can follow, and seeds a demo directory so the whole chain can be tried locally.
Groups are named ``db_<role>``; ClickHouse maps them to roles by itself (see cluster.xml).
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import yaml
from ldap3 import ALL, MODIFY_ADD, MODIFY_DELETE, Connection, Server
from ldap3.core.exceptions import (
    LDAPEntryAlreadyExistsResult,
    LDAPException,
    LDAPNoSuchObjectResult,
)

from ..topology import Topology
from .model import AccessModel

PREFIX = "db_"
PEOPLE_OU = "ou=people"
GROUPS_OU = "ou=groups"


class DirectoryError(Exception):
    pass


def connect(topo: Topology) -> Connection:
    ldap = topo.ldap
    if ldap is None:
        raise DirectoryError("topology.yaml has no ldap section")
    server = Server(os.environ.get("LDAP_HOST", "localhost"), port=ldap.port, get_info=ALL)
    try:
        return Connection(
            server,
            user=f"cn=admin,{ldap.base_dn}",
            password=os.environ.get("LDAP_ADMIN_PASSWORD", "charlie"),
            auto_bind=True,
            raise_exceptions=True,
        )
    except LDAPException as e:
        raise DirectoryError(f"cannot reach the directory: {e}") from e


def person_dn(base_dn: str, user: str) -> str:
    return f"cn={user},{PEOPLE_OU},{base_dn}"


def group_dn(base_dn: str, role: str) -> str:
    return f"cn={PREFIX}{role},{GROUPS_OU},{base_dn}"


def _placeholder(base_dn: str) -> str:
    # groupOfNames must have a member. This entry does not exist, so it never matches a login.
    return person_dn(base_dn, "nobody")


def memberships(conn: Connection, base_dn: str) -> dict[str, set[str]]:
    """{login: {role, ...}} for every person that exists, from the db_* groups."""
    conn.search(f"{PEOPLE_OU},{base_dn}", "(objectClass=inetOrgPerson)", attributes=["cn"])
    people = {str(e.cn.value) for e in conn.entries}
    conn.search(
        f"{GROUPS_OU},{base_dn}",
        f"(&(objectClass=groupOfNames)(cn={PREFIX}*))",
        attributes=["cn", "member"],
    )
    out: dict[str, set[str]] = {}
    for g in conn.entries:
        role = str(g.cn.value).removeprefix(PREFIX)
        for dn in g.member.values:
            head = str(dn).split(",", 1)[0]
            user = head.removeprefix("cn=") if head.startswith("cn=") else ""
            if user in people:
                out.setdefault(user, set()).add(role)
    for user in people:
        out.setdefault(user, set())
    return out


def seed(conn: Connection, base_dn: str, model: AccessModel, people_file: Path) -> dict:
    """Create the OUs, one group per role, and the demo people. Idempotent."""
    spec = yaml.safe_load(people_file.read_text())["people"]
    created = {"ou": 0, "groups": 0, "people": 0, "memberships": 0}

    def add(dn: str, classes: list[str], attrs: dict, key: str) -> bool:
        try:
            conn.add(dn, classes, attrs)
        except LDAPEntryAlreadyExistsResult:
            return False
        created[key] += 1
        return True

    for ou in (PEOPLE_OU, GROUPS_OU):
        add(f"{ou},{base_dn}", ["organizationalUnit"], {"ou": ou.removeprefix("ou=")}, "ou")
    for role in model.role_names():
        add(
            group_dn(base_dn, role),
            ["groupOfNames"],
            {"cn": PREFIX + role, "member": _placeholder(base_dn)},
            "groups",
        )
    known = set(model.role_names())
    for user, p in spec.items():
        if add_person(conn, base_dn, user, p["name"], p["password"]):
            created["people"] += 1
        for role in p.get("groups", []):
            if role not in known:
                raise DirectoryError(f"{user}: group {role!r} is not a role of the model")
            if add_member(conn, base_dn, user, role):
                created["memberships"] += 1
    return created


def add_person(conn: Connection, base_dn: str, user: str, name: str, password: str) -> bool:
    try:
        conn.add(
            person_dn(base_dn, user),
            ["inetOrgPerson"],
            {
                "cn": user,
                "sn": name.split()[-1],
                "displayName": name,
                "uid": user,
                "userPassword": password,
            },
        )
    except LDAPEntryAlreadyExistsResult:
        return False
    return True


def remove_person(conn: Connection, base_dn: str, user: str) -> None:
    """Delete the account and every group membership, as an IdP does when someone leaves."""
    for role in memberships(conn, base_dn).get(user, set()):
        remove_member(conn, base_dn, user, role)
    with contextlib.suppress(LDAPNoSuchObjectResult):  # already gone is fine
        conn.delete(person_dn(base_dn, user))


def add_member(conn: Connection, base_dn: str, user: str, role: str) -> bool:
    return _modify(conn, base_dn, user, role, MODIFY_ADD)


def remove_member(conn: Connection, base_dn: str, user: str, role: str) -> bool:
    return _modify(conn, base_dn, user, role, MODIFY_DELETE)


def _modify(conn: Connection, base_dn: str, user: str, role: str, op: str) -> bool:
    try:
        conn.modify(group_dn(base_dn, role), {"member": [(op, [person_dn(base_dn, user)])]})
    except LDAPException as e:
        # already a member / not a member: the end state is what was asked for
        if "attributeOrValueExists" in str(e) or "noSuchAttribute" in str(e):
            return False
        raise DirectoryError(str(e)) from e
    return True
