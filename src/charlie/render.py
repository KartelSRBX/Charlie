"""Render deploy/ from topology.yaml. Pure and deterministic: same input, same bytes."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .topology import Topology

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"
STATIC = ROOT / "config"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATES),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render(topo: Topology) -> dict[str, str]:
    """Return {relative path under deploy/: file content}."""
    env = _env()
    ctx = {"t": topo}
    files: dict[str, str] = {
        "docker-compose.yml": env.get_template("compose.yml.j2").render(**ctx),
        "prometheus/prometheus.yml": env.get_template("prometheus.yml.j2").render(**ctx),
        "config/cluster.xml": env.get_template("cluster.xml.j2").render(**ctx),
    }
    for node in topo.nodes:
        files[f"config/{node.name}/macros.xml"] = env.get_template("macros.xml.j2").render(
            node=node, **ctx
        )
    for keeper in topo.keeper_nodes:
        files[f"config/{keeper.name}/keeper.xml"] = env.get_template("keeper.xml.j2").render(
            keeper=keeper, **ctx
        )
    for src in sorted(STATIC.glob("*.xml")):
        files[f"config/{src.name}"] = src.read_text()
    return files


def write(topo: Topology, out: Path) -> list[str]:
    """Write the rendered tree to ``out``. Returns paths whose content changed or is new.

    Files that no longer belong to the topology (a removed node, say) are deleted, so shrinking
    topology.yaml never leaves a stale config behind.
    """
    files = render(topo)
    changed = []
    for rel, content in files.items():
        path = out / rel
        if not path.exists() or path.read_text() != content:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            changed.append(rel)
    for path in sorted(out.rglob("*")):
        if path.is_file() and str(path.relative_to(out)) not in files:
            path.unlink()
            changed.append(f"{path.relative_to(out)} (removed)")
    for d in sorted((p for p in out.rglob("*") if p.is_dir()), reverse=True):
        if not any(d.iterdir()):
            d.rmdir()
    return changed


def drift(topo: Topology, out: Path) -> list[str]:
    """Paths under ``out`` that differ from what topology.yaml would render."""
    files = render(topo)
    bad = []
    for rel, content in files.items():
        path = out / rel
        if not path.exists():
            bad.append(f"{rel} (missing)")
        elif path.read_text() != content:
            bad.append(f"{rel} (differs)")
    for path in sorted(out.rglob("*")):
        if path.is_file() and str(path.relative_to(out)) not in files:
            bad.append(f"{path.relative_to(out)} (unexpected)")
    return bad
