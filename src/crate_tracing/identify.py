"""Best-effort identification of what a native component is and which version.

Every identification carries the method that produced it, so a consumer can
weigh a version read from a crate's semver build metadata (strong) against one
guessed from a file name in a URL (weaker).
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

_VERSION = r"v?(?P<version>\d+(?:\.\d+){1,3}(?:[-.]?(?:rc|alpha|beta|pre)[.-]?\d*)?)"
_ARCHIVE_EXT = re.compile(
    r"\.(?:tar\.gz|tar\.xz|tar\.bz2|tar\.zst|tgz|tbz2|txz|zip|gz|xz|bz2|zst|a|so|dylib|lib|dll|whl|jar)$",
    re.I,
)
_GITHUB_RELEASE = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/releases/download/(?P<tag>[^/]+)/(?P<asset>[^/]+)$")
_GITHUB_ARCHIVE = re.compile(
    r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/archive/(?:refs/tags/)?(?P<tag>.+?)\.(?:tar\.gz|zip)$"
)
_NAME_VERSION = re.compile(r"^(?P<name>[A-Za-z][A-Za-z0-9_+.-]*?)[-_]" + _VERSION + r"(?:[-_.].*)?$")
_AT_VERSION = re.compile(r"(?P<name>[A-Za-z][A-Za-z0-9_.-]*)@" + _VERSION)
_TAG_VERSION = re.compile(r"^(?:[A-Za-z_-]*?[-_])?" + _VERSION + r"$")

# Crates whose name is not the native library's name.
_SRC_CRATE_NATIVE = {"openssl-src": "openssl", "boringssl-src": "boringssl"}


def _identity(name, version, method, confidence) -> dict:
    out = {"name": name, "version": version, "method": method, "confidence": confidence}
    return {k: v for k, v in out.items() if v is not None}


def from_url(url: str) -> dict | None:
    parts = urlsplit(url)
    path = unquote(parts.path)
    host = (parts.hostname or "").lower()

    if host == "github.com":
        m = _GITHUB_RELEASE.match(path) or _GITHUB_ARCHIVE.match(path)
        if m:
            tag = _TAG_VERSION.match(m.group("tag"))
            return _identity(
                f"github.com/{m.group('owner')}/{m.group('repo')}",
                tag.group("version") if tag else m.group("tag"),
                "github-release-tag" if "releases" in path else "github-archive-tag",
                "high" if tag else "medium",
            )

    m = _AT_VERSION.search(path)
    if m:
        return _identity(m.group("name"), m.group("version"), "url-name@version", "medium")

    segments = [s for s in path.split("/") if s]
    if segments:
        leaf = _ARCHIVE_EXT.sub("", segments[-1])
        m = _NAME_VERSION.match(leaf)
        if m:
            return _identity(m.group("name"), m.group("version"), "url-filename", "medium")
        for seg in reversed(segments[:-1]):
            t = _TAG_VERSION.match(seg)
            if t:
                return _identity(leaf or None, t.group("version"), "url-path-version", "low")
    return None


def from_crate(name: str, version: str, links: str | None) -> dict | None:
    """The bundled native version a -sys/-src crate declares, if any.

    Crates that vendor a native library conventionally put its version in the
    semver build metadata: openssl-src 300.5.0+3.5.0, zstd-sys
    2.1.2+zstd.1.5.7, libgit2-sys 0.18.8+1.9.7.
    """
    if "+" not in version:
        return None
    meta = version.split("+", 1)[1]
    m = re.match(r"^(?:(?P<name>[A-Za-z][A-Za-z0-9_-]*)[.-])?(?P<version>\d+(?:\.\d+)*.*)$", meta)
    if not m:
        return None
    native = m.group("name")
    if native is None:
        native = _SRC_CRATE_NATIVE.get(name)
    if native is None and links:
        native = links
    if native is None:
        native = name.removesuffix("-sys").removesuffix("-src")
    return _identity(native, m.group("version"), "semver-build-metadata", "high")


def from_archive_root(root: str) -> dict | None:
    """Identify from an archive's top-level directory, e.g. openssl-3.5.0/."""
    m = _NAME_VERSION.match(root.strip("/"))
    if m:
        return _identity(m.group("name"), m.group("version"), "archive-root-dir", "medium")
    return None
