"""The corpus (what to measure) and the profiles (where to measure it)."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROUTES = ("A", "B", "C", "D", "unresolved", "none", "fail")
_ID = re.compile(r"^[a-z0-9][a-z0-9.+_-]*$")


@dataclass(frozen=True)
class Profile:
    name: str
    image: str
    description: str = ""
    apt_packages: tuple[str, ...] = ()


@dataclass(frozen=True)
class Measurement:
    id: str
    crate: str
    version: str
    features: tuple[str, ...] = ()
    default_features: bool = True
    profiles: tuple[str, ...] = ()
    expect: dict[str, tuple[str, ...]] = field(default_factory=dict)
    notes: str = ""

    @property
    def purl(self) -> str:
        return cargo_purl(self.crate, self.version)

    @property
    def requirement(self) -> str:
        # Cargo ignores (and warns about) build metadata in a requirement, and
        # crates.io never publishes two versions differing only in it.
        return "=" + self.version.split("+", 1)[0]


def cargo_purl(name: str, version: str) -> str:
    # purl percent-encodes '+' in versions.
    return f"pkg:cargo/{name}@{version.replace('+', '%2B')}"


def load_profiles(path: Path) -> dict[str, Profile]:
    data = tomllib.loads(path.read_text())
    out = {}
    for name, p in data.get("profiles", {}).items():
        if "@sha256:" not in p["image"]:
            raise ValueError(f"profile {name}: image must be pinned by digest")
        out[name] = Profile(
            name=name,
            image=p["image"],
            description=p.get("description", ""),
            apt_packages=tuple(p.get("apt_packages", ())),
        )
    return out


def load_corpus(path: Path, profiles: dict[str, Profile]) -> list[Measurement]:
    data = tomllib.loads(path.read_text())
    seen: set[str] = set()
    out = []
    for m in data.get("measurement", []):
        mid = m["id"]
        if not _ID.match(mid):
            raise ValueError(f"measurement id {mid!r} is not a safe slug")
        if mid in seen:
            raise ValueError(f"duplicate measurement id {mid!r}")
        seen.add(mid)
        names = tuple(m.get("profiles", profiles.keys()))
        for n in names:
            if n not in profiles:
                raise ValueError(f"{mid}: unknown profile {n!r}")
        expect = {k: tuple(v) for k, v in m.get("expect", {}).items()}
        for k, routes in expect.items():
            bad = [r for r in routes if r not in ROUTES]
            if bad:
                raise ValueError(f"{mid}: expect.{k} has unknown routes {bad}")
        out.append(
            Measurement(
                id=mid,
                crate=m["crate"],
                version=m["version"],
                features=tuple(m.get("features", ())),
                default_features=m.get("default_features", True),
                profiles=names,
                expect=expect,
                notes=m.get("notes", ""),
            )
        )
    return out
