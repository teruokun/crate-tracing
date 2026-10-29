"""Read the evidence files measure.sh posted as artifacts.

Every file here is also an artifact record in the ledger; `verify` checks the
copy on disk against the hash the ledger signed before anything reads it.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .ledger import Ledger

UNIT_DIR = re.compile(r"^(?P<name>.+)-[0-9a-f]{16}$")
_CARGO_DIRECTIVE = re.compile(r"^cargo::?(?P<key>[A-Za-z0-9_-]+)=(?P<value>.*)$")
_FAILED_BUILD = re.compile(r"failed to run custom build command for `(?P<name>\S+) v(?P<version>\S+)")

# Tools that only probe or link; see tool_runs.
COMPILERS = {"cc", "gcc", "g++", "c++", "clang", "clang++"}
PROBES = {"pkg-config"}


@dataclass
class LinkLib:
    unit: str
    kind: str
    name: str
    path: str
    package: str
    real_path: str
    real_package: str


@dataclass
class ToolRun:
    package: str
    version: str
    tool: str
    mode: str  # "compile" when a compiler was given -c
    args: str


@dataclass
class Evidence:
    root: Path  # <output>/artifacts/ct
    status: dict[str, int] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    dpkg: dict[str, str] = field(default_factory=dict)  # package -> version
    metadata: dict | None = None
    lock: dict | None = None
    link_libs: list[LinkLib] = field(default_factory=list)
    tools: list[ToolRun] = field(default_factory=list)
    build_outputs: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    mismatched: list[str] = field(default_factory=list)  # files whose hash != ledger

    def file(self, name: str) -> Path:
        return self.root / name


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(output_dir: Path, ledger: Ledger) -> list[str]:
    """Artifact names whose file on disk does not match the ledger's sha256."""
    bad = []
    for a in ledger.artifacts:
        want = a.hashes.get("sha256")
        path = output_dir / "artifacts" / a.name
        if want is None or not path.is_file() or _sha256(path) != want:
            bad.append(a.name)
    return bad


def _read(path: Path) -> str | None:
    return path.read_text(errors="replace") if path.is_file() else None


def _kv(text: str | None) -> dict[str, str]:
    out = {}
    for line in (text or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"')
    return out


def load(output_dir: Path, ledger: Ledger) -> Evidence:
    ev = Evidence(root=output_dir / "artifacts" / "ct")
    ev.mismatched = verify(output_dir, ledger)

    for k, v in _kv(_read(ev.file("status.env"))).items():
        try:
            ev.status[k] = int(v)
        except ValueError:
            pass
    ev.env = _kv(_read(ev.file("env.txt")))

    for line in (_read(ev.file("dpkg.tsv")) or "").splitlines():
        cols = line.split("\t")
        if len(cols) >= 2:
            ev.dpkg[cols[0]] = cols[1]

    if (text := _read(ev.file("metadata.json"))) is not None:
        try:
            ev.metadata = json.loads(text)
        except json.JSONDecodeError:
            ev.metadata = None
    if (text := _read(ev.file("Cargo.lock"))) is not None:
        ev.lock = tomllib.loads(text)

    for line in (_read(ev.file("syslibs.tsv")) or "").splitlines():
        cols = (line.split("\t") + [""] * 7)[:7]
        ev.link_libs.append(LinkLib(*cols))

    for line in (_read(ev.file("tools.tsv")) or "").splitlines():
        cols = (line.split("\t", 4) + [""] * 5)[:5]
        ev.tools.append(ToolRun(*cols))

    bs = ev.root / "buildscript"
    if bs.is_dir():
        for unit in sorted(bs.iterdir()):
            out = unit / "output"
            directives = []
            for line in (_read(out) or "").splitlines():
                m = _CARGO_DIRECTIVE.match(line.strip())
                if m:
                    directives.append((m.group("key"), m.group("value")))
            ev.build_outputs[unit.name] = directives
    return ev


def unit_package(unit: str) -> str:
    m = UNIT_DIR.match(unit)
    return m.group("name") if m else unit


def lock_checksums(lock: dict | None) -> dict[tuple[str, str], str]:
    out = {}
    for p in (lock or {}).get("package", []):
        if "checksum" in p:
            out[(p["name"], p["version"])] = p["checksum"]
    return out


# rustc links through `cc`; its calls name these (args are cut to 300 chars, so
# the first, symbols.o, is the one reliably present).
_RUSTC_LINK = ("symbols.o", ".rcgu.o", "-nodefaultlibs")


def tool_runs(ev: Evidence) -> list[ToolRun]:
    """Tool runs by build scripts, without rustc's own linker calls."""
    return [t for t in ev.tools if t.package and not any(s in t.args for s in _RUSTC_LINK)]


def failure(ev: Evidence) -> dict | None:
    if ev.status.get("build_exit", 0) == 0:
        return None
    log = _read(ev.file("build.log")) or ""
    out: dict = {}
    matches = list(_FAILED_BUILD.finditer(log))
    if matches:
        m = matches[-1]
        out["package"] = m.group("name")
        out["version"] = m.group("version")
    lines = log.splitlines()
    if "--- stderr" in lines:
        start = len(lines) - 1 - lines[::-1].index("--- stderr")
        tail = [ln for ln in lines[start + 1 :] if ln.strip()][:20]
    else:
        tail = [ln for ln in lines if ln.strip()][-20:]
    out["excerpt"] = tail
    return out
