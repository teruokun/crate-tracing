"""Read `warden inspect --json` into requests, artifacts and phases.

The relay writes one channel per request: an `open` record (method, URL),
`checkpoint`s for headers and request bodies, then a `close` carrying the
response body's hash block and status. A posted artifact is the same shape but
ends in an `artifact` record instead. Every later record names its channel by
`open_sig`, the open record's signature. Records are strictly ordered, so the
open record's position against the marker artifacts tells which phase a
request belongs to.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

PHASES = ("setup", "fetch", "build", "collect")
MARKER_PREFIX = "ct/marker/"
_MARKER_PHASE = {"begin-fetch": "fetch", "begin-build": "build", "begin-collect": "collect"}

# Byte length of each hash algorithm the relay may list in the header.
HASH_SIZES = {
    "blake2b_256": 32,
    "blake2b-256": 32,
    "sha256": 32,
    "sha512": 64,
    "sha1": 20,
    "md5": 16,
}


@dataclass
class Request:
    seq: int  # ledger sequence of the open record
    method: str
    url: str
    phase: str
    status: int | None = None
    bytes_in: int = 0
    bytes_out: int = 0
    hashes: dict[str, str] = field(default_factory=dict)
    complete: bool = False

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").lower()

    @property
    def path(self) -> str:
        return urlsplit(self.url).path


@dataclass
class Artifact:
    seq: int
    name: str
    size: int
    hashes: dict[str, str]


@dataclass
class Ledger:
    valid: bool
    summary: dict
    environment: dict | None
    requests: list[Request]
    artifacts: list[Artifact]
    markers: dict[str, int]  # marker name -> ledger seq

    def artifact(self, name: str) -> Artifact | None:
        for a in self.artifacts:
            if a.name == name:
                return a
        return None

    def in_phase(self, phase: str) -> list[Request]:
        return [r for r in self.requests if r.phase == phase]


def split_hash_block(block_hex: str, names: list[str]) -> dict[str, str]:
    if not block_hex:
        return {}
    sizes = [HASH_SIZES.get(n) for n in names]
    if None in sizes or sum(sizes) * 2 != len(block_hex):  # type: ignore[arg-type]
        return {"hash_block": block_hex}
    out, pos = {}, 0
    for n, size in zip(names, sizes, strict=True):
        out[n.replace("-", "_")] = block_hex[pos : pos + size * 2]  # type: ignore[operator]
        pos += size * 2  # type: ignore[operator]
    return out


def load(path: Path) -> Ledger:
    return parse(json.loads(path.read_text()))


def parse(report: dict) -> Ledger:
    header = report.get("header", {})
    hash_names = header.get("hashes") or []
    records = report.get("records") or []

    opens: dict[str, dict] = {}
    artifact_channels: set[str] = set()
    artifacts: list[Artifact] = []
    markers: dict[str, int] = {}

    for rec in records:
        if rec["type"] == "open":
            opens[rec["signature"]] = rec
        elif rec["type"] == "artifact":
            artifact_channels.add(rec.get("open_sig", ""))
            name = (rec.get("metadata") or {}).get("name", "")
            artifacts.append(
                Artifact(
                    seq=rec["seq"],
                    name=name,
                    size=rec.get("bytes", 0),
                    hashes=split_hash_block(rec.get("hash_block", ""), hash_names),
                )
            )
            if name.startswith(MARKER_PREFIX):
                markers[name[len(MARKER_PREFIX) :]] = rec["seq"]

    def phase_of(seq: int) -> str:
        phase = "setup"
        for marker, p in _MARKER_PHASE.items():
            if marker in markers and seq > markers[marker]:
                phase = p
        return phase

    requests: dict[str, Request] = {}
    for sig, rec in opens.items():
        if sig in artifact_channels:
            continue
        meta = rec.get("metadata") or {}
        url = meta.get("url", "")
        if not url:
            continue  # the environment record's channel, or a non-HTTP open
        requests[sig] = Request(
            seq=rec["seq"],
            method=meta.get("method", ""),
            url=url,
            phase=phase_of(rec["seq"]),
        )

    for rec in records:
        req = requests.get(rec.get("open_sig", ""))
        if req is None:
            continue
        direction = rec.get("direction")
        is_body = rec.get("schema", "").endswith("http-body.json")
        if rec["type"] == "close":
            req.complete = True
            meta = rec.get("metadata") or {}
            if "status" in meta:
                req.status = int(meta["status"])
            if direction == "in":
                req.bytes_in += rec.get("bytes", 0)
                req.hashes = split_hash_block(rec.get("hash_block", ""), hash_names)
        elif rec["type"] == "checkpoint" and is_body and direction == "out":
            req.bytes_out += rec.get("bytes", 0)

    return Ledger(
        valid=bool(report.get("summary", {}).get("valid")),
        summary=report.get("summary", {}),
        environment=report.get("environment"),
        requests=sorted(requests.values(), key=lambda r: r.seq),
        artifacts=artifacts,
        markers=markers,
    )
