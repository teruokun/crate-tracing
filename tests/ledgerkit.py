"""Build `warden inspect --json` reports shaped like the relay writes them.

A request is an open record, a request-headers checkpoint, a response-headers
checkpoint and a close carrying the body hash; an artifact is open, headers
checkpoint and an artifact record. The output directory gets the artifact files
(and optional captures) at the paths warden uses, so the analyzer sees what it
would after a real run.
"""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

HASHES = ["blake2b_256", "sha256", "sha1", "md5"]
SCHEMAS = [
    "https://github.com/buildwarden/buildwarden/schemas/http-open.json",
    "https://github.com/buildwarden/buildwarden/schemas/http-headers.json",
    "https://github.com/buildwarden/buildwarden/schemas/http-body.json",
    "https://github.com/buildwarden/buildwarden/schemas/artifact.json",
]


def hash_block(data: bytes, sha256: str | None = None) -> str:
    return (
        hashlib.blake2b(data, digest_size=32).hexdigest()
        + (sha256 or hashlib.sha256(data).hexdigest())
        + hashlib.sha1(data).hexdigest()
        + hashlib.md5(data).hexdigest()
    )


class LedgerBuilder:
    def __init__(self, output_dir: Path):
        self.out = output_dir
        self.records: list[dict] = []
        self.capture_seq = 0
        self.valid = True

    def _rec(self, type_: str, schema: int, size: int = 0, open_sig=None, block=None, meta=None) -> str:
        seq = len(self.records) + 1
        rec = {
            "seq": seq,
            "type": type_,
            "bytes": abs(size),
            "schema": SCHEMAS[schema],
            "prev_sig": f"sig-{seq - 1}",
            "signature": f"sig-{seq}",
        }
        if size:
            rec["direction"] = "in" if size > 0 else "out"
        if open_sig:
            rec["open_sig"] = open_sig
        if block:
            rec["hash_block"] = block
        if meta is not None:
            rec["metadata"] = meta
        self.records.append(rec)
        return rec["signature"]

    def request(
        self,
        url: str,
        body: bytes = b"",
        *,
        method: str = "GET",
        status: int = 200,
        sha256: str | None = None,
        capture: bool = False,
        request_headers: dict[str, str] | None = None,
        response_headers: dict[str, str] | None = None,
    ) -> int:
        sig = self._rec("open", 0, meta={"method": method, "url": url, "protocol": "HTTP/1.1"})
        seq = len(self.records)
        self._rec("checkpoint", 1, -64, sig, hash_block(b"req"), {"headers": []})
        self._rec("checkpoint", 1, 64, sig, hash_block(b"resp"), {"headers": []})
        size = len(body) if sha256 is None else max(len(body), 1)
        self._rec("close", 2, size, sig, hash_block(body, sha256), {"status": status})
        if capture:
            self.capture_seq += 1
            parts = urlsplit(url)
            slug = parts.hostname + (
                "-" + parts.path.lstrip("/").replace("/", "-")[:80] if parts.path.strip("/") else ""
            )
            base = f"{self.capture_seq:03d}-{method}-{slug}"
            caps = self.out / "captures"
            caps.mkdir(parents=True, exist_ok=True)
            (caps / base).write_bytes(body)
            rq = "".join(f"{k}: {v}\r\n" for k, v in (request_headers or {}).items())
            (caps / f"{base}.request-headers").write_text(f"{method} {parts.path} HTTP/1.1\r\n{rq}\r\n")
            rs = "".join(f"{k}: {v}\r\n" for k, v in (response_headers or {}).items())
            (caps / f"{base}.response-headers").write_text(f"HTTP/1.1 {status} X\r\n{rs}\r\n")
        return seq

    def artifact(self, name: str, data: bytes) -> int:
        path = self.out / "artifacts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        sig = self._rec("open", 0, meta={"method": "POST", "url": f"http://artifacts/{name}", "protocol": "HTTP/1.1"})
        self._rec("checkpoint", 1, -64, sig, hash_block(b"req"), {"headers": []})
        self._rec("artifact", 3, -len(data), sig, hash_block(data), {"name": name, "context": {}})
        return len(self.records)

    def marker(self, name: str) -> int:
        return self.artifact(f"ct/marker/{name}", name.encode() + b"\n")

    def report(self) -> dict:
        opens = sum(1 for r in self.records if r["type"] == "open")
        return {
            "header": {"hashes": HASHES, "schemas": SCHEMAS, "valid": True},
            "environment": {"type": "oci-image/v1", "digest": "sha256:" + "ab" * 32},
            "records": self.records,
            "summary": {
                "total_records": len(self.records),
                "requests": opens,
                "valid": self.valid,
            },
        }


def measured_run(output_dir: Path, fixture: Path, build_requests=()) -> dict:
    """A full run: setup, a fetch of every crate in the fixture's Cargo.lock,
    the given build-phase requests, then the fixture's evidence as artifacts."""
    lb = LedgerBuilder(output_dir)
    lb.request("http://cwd/probe/Cargo.toml", b"[package]")
    lb.marker("begin-fetch")
    lock = tomllib.loads((fixture / "ct" / "Cargo.lock").read_text())
    lb.request("https://index.crates.io/config.json", b"{}")
    for pkg in lock["package"]:
        if "checksum" not in pkg:
            continue
        lb.request(f"https://index.crates.io/x/{pkg['name']}", b"index")
        lb.request(
            f"https://static.crates.io/crates/{pkg['name']}/{pkg['version']}/download",
            b"crate",
            sha256=pkg["checksum"],
        )
    lb.marker("begin-build")
    for req in build_requests:
        lb.request(**req)
    lb.marker("begin-collect")
    for f in sorted((fixture / "ct").rglob("*")):
        if f.is_file():
            lb.artifact("ct/" + f.relative_to(fixture / "ct").as_posix(), f.read_bytes())
    lb.marker("end")
    return lb.report()
