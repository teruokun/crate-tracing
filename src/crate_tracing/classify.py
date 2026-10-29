"""Sort requests by who they are for: the registry, the OS, the relay, or else."""

from __future__ import annotations

import re

from .ledger import Request

RELAY = "relay"  # the relay's own endpoints (build script, context, artifacts)
REGISTRY = "crates-registry"
TOOLCHAIN = "rust-toolchain"
OS_PACKAGES = "os-packages"
EXTERNAL = "external"

_EXACT = {
    "artifacts": RELAY,
    "cwd": RELAY,
    "index.crates.io": REGISTRY,
    "static.crates.io": REGISTRY,
    "crates.io": REGISTRY,
    "static.rust-lang.org": TOOLCHAIN,
    "sh.rustup.rs": TOOLCHAIN,
    "deb.debian.org": OS_PACKAGES,
    "security.debian.org": OS_PACKAGES,
    "snapshot.debian.org": OS_PACKAGES,
}
_SUFFIX = (
    (".ubuntu.com", OS_PACKAGES),
    (".debian.org", OS_PACKAGES),
)

# static.crates.io serves `.crate` files at either of these paths.
_CRATE_PATHS = (
    re.compile(r"^/crates/(?P<name>[^/]+)/(?P<version>[^/]+)/download$"),
    re.compile(r"^/crates/(?P<name>[^/]+)/(?P=name)-(?P<version>.+)\.crate$"),
)


def classify(req: Request) -> str:
    host = req.host
    if host in _EXACT:
        return _EXACT[host]
    for suffix, cls in _SUFFIX:
        if host.endswith(suffix):
            return cls
    return EXTERNAL


def crate_download(req: Request) -> tuple[str, str] | None:
    """(name, version) when the request downloads a .crate file."""
    if req.host != "static.crates.io":
        return None
    for pattern in _CRATE_PATHS:
        m = pattern.match(req.path)
        if m:
            return m.group("name"), m.group("version")
    return None
