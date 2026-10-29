"""Find what the relay captured for a request (`--capture all`).

The relay names captures `<seq>-<METHOD>-<host>-<path>` (path's slashes to
dashes, cut to 80 characters, unsafe characters to `_`), with a
`.request-headers`, `.response-headers` or `.request-body` suffix for anything
but the response body. Its sequence counts proxied requests only, so we match
by name and take same-named captures in ledger order.

Captured payloads came from the internet: they are only ever read here, never
extracted or executed.
"""

from __future__ import annotations

import re
import tarfile
import zipfile
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

from .ledger import Request

_SAFE = re.compile(r"[^A-Za-z0-9._-]")
_SEQ = re.compile(r"^(?P<seq>\d+)-(?P<rest>.+)$")
_SUFFIXES = ("request-headers", "response-headers", "request-body")


def capture_key(method: str, url: str) -> str:
    parts = urlsplit(url)
    slug = (parts.netloc or "").split(":", 1)[0]  # the relay drops the port
    p = parts.path.lstrip("/").replace("/", "-")[:80]
    if p:
        slug += "-" + p
    return _SAFE.sub("_", f"{method}-{slug}")


class Captures:
    def __init__(self, output_dir: Path):
        self.dir = output_dir / "captures"
        # key -> list of (seq, {suffix: path}), sorted by seq
        index: dict[str, dict[int, dict[str, Path]]] = defaultdict(dict)
        if self.dir.is_dir():
            for f in self.dir.iterdir():
                m = _SEQ.match(f.name)
                if not m:
                    continue
                rest, suffix = m.group("rest"), ""
                for s in _SUFFIXES:
                    if rest.endswith("." + s):
                        rest, suffix = rest[: -len(s) - 1], s
                        break
                index[rest].setdefault(int(m.group("seq")), {})[suffix] = f
        self._index = {k: [v[s] for s in sorted(v)] for k, v in index.items()}
        self._taken: dict[str, int] = defaultdict(int)

    def take(self, req: Request) -> dict[str, Path]:
        """The capture files for `req`; call in ledger order."""
        key = capture_key(req.method, req.url)
        entries = self._index.get(key, [])
        i = self._taken[key]
        if i >= len(entries):
            return {}
        self._taken[key] += 1
        return entries[i]


def header(path: Path | None, name: str) -> str | None:
    if path is None or not path.is_file():
        return None
    want = name.lower() + ":"
    for line in path.read_bytes().decode("latin-1").splitlines():
        if line.lower().startswith(want):
            return line.split(":", 1)[1].strip()
    return None


def archive_root(path: Path | None, limit: int = 200) -> str | None:
    """The single top-level directory of a tar or zip payload, if it has one."""
    if path is None or not path.is_file():
        return None
    names: list[str] = []
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as z:
                names = z.namelist()[:limit]
        else:
            with tarfile.open(path, "r:*") as t:
                for i, member in enumerate(t):
                    if i >= limit:
                        break
                    names.append(member.name)
    except (tarfile.TarError, zipfile.BadZipFile, OSError, EOFError):
        return None
    roots = {n.lstrip("./").split("/", 1)[0] for n in names if n.strip("./")}
    return roots.pop() if len(roots) == 1 else None
