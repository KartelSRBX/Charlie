"""Connections to the running cluster from the host."""

from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from .topology import Topology

# The driver logs every failed HTTP call at ERROR; here failures are expected and handled.
logging.getLogger("clickhouse_connect").setLevel(logging.CRITICAL)

USER = os.environ.get("CH_ADMIN_USER", "admin")
PASSWORD = os.environ.get("CH_ADMIN_PASSWORD", "charlie")
HOST = os.environ.get("CH_HOST", "localhost")


@dataclass(frozen=True)
class Endpoint:
    name: str
    port: int


def endpoints(topo: Topology) -> list[Endpoint]:
    return [Endpoint(n.name, n.http_port) for n in topo.nodes]


def connect(port: int, *, timeout: int = 10, **settings: object) -> Client:
    return clickhouse_connect.get_client(
        host=HOST,
        port=port,
        username=USER,
        password=PASSWORD,
        connect_timeout=3,
        send_receive_timeout=timeout,
        settings=settings,
    )


def first_alive(topo: Topology, **settings: object) -> Client:
    """A client for the first node that answers, so admin commands survive a dead node."""
    last: Exception | None = None
    for ep in endpoints(topo):
        try:
            c = connect(ep.port, **settings)
            c.command("SELECT 1")
            return c
        except Exception as e:
            last = e
    raise ConnectionError(f"no ClickHouse node answers on {HOST}: {last}")


def keeper_command(port: int, word: str, timeout: float = 2.0) -> str:
    """Send a ZooKeeper four-letter-word command to a Keeper node."""
    with socket.create_connection((HOST, port), timeout=timeout) as s:
        s.sendall(word.encode())
        s.shutdown(socket.SHUT_WR)
        chunks = []
        while data := s.recv(4096):
            chunks.append(data)
    return b"".join(chunks).decode()
