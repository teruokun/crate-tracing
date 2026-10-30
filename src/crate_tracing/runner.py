"""Drive `warden`: build one measurement's context, then inspect the ledger."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .context import write_context
from .corpus import Measurement, Profile


@dataclass
class Run:
    dir: Path
    output: Path  # warden's output directory
    inspect_json: Path
    build_exit: int
    inspect_exit: int
    wall_seconds: float


def warden_info(warden: str) -> dict | None:
    """Which BuildWarden produced a ledger, precisely enough to rebuild it.

    Release builds embed only `main.version`; the commit comes from the VCS
    stamp Go writes into every binary built from a git checkout (`go version
    -m`). The binary's SHA-256 identifies it even when neither is present.
    The relay, holder and netns images are pinned inside the binary, so this
    pins them too.
    """
    path = shutil.which(warden) or (warden if Path(warden).is_file() else None)
    if path is None:
        return None
    path = os.path.realpath(path)
    info: dict = {"path": path, "binary_sha256": _sha256(Path(path))}
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, check=False).stdout
        info["version"] = out.strip().removeprefix("warden version ").strip() or None
    except OSError:
        info["version"] = None
    info.update(_go_buildinfo(path))
    return info


def _go_buildinfo(path: str) -> dict:
    if shutil.which("go") is None:
        return {}
    out = subprocess.run(["go", "version", "-m", path], capture_output=True, text=True, check=False).stdout
    keys = {"vcs.revision": "revision", "vcs.time": "revision_time", "vcs.modified": "modified"}
    info: dict = {}
    for line in out.splitlines():
        cols = line.split("\t")
        if len(cols) >= 3 and cols[1] == "mod":
            info["module"] = cols[2]
            info["module_version"] = cols[3] if len(cols) > 3 else None
        elif len(cols) >= 3 and cols[1] == "build" and "=" in cols[2]:
            k, v = cols[2].split("=", 1)
            if k in keys:
                info[keys[k]] = v == "true" if k == "vcs.modified" else v
        elif line.startswith(path + ": "):
            info["go"] = line.split(": ", 1)[1]
    return info


def output_bytes(out: Path) -> int:
    """Size of warden's output directory, payload captures included."""
    return sum(f.stat().st_size for f in out.rglob("*") if f.is_file())


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run(
    m: Measurement,
    p: Profile,
    run_dir: Path,
    *,
    warden: str = "warden",
    timeout: str = "90m",
    lockfile: Path | None = None,
    echo: bool = False,
    extra_args: tuple[str, ...] = (),
) -> Run:
    if shutil.which(warden) is None and not Path(warden).is_file():
        raise FileNotFoundError(f"warden not found: {warden}")
    run_dir.mkdir(parents=True, exist_ok=True)
    ctx = write_context(run_dir / "context", m, p, lockfile)
    out = run_dir / "warden-output"
    if out.exists():
        shutil.rmtree(out)

    cmd = [
        warden,
        "build",
        str(ctx),
        "-o",
        str(out),
        "--capture",
        "all",
        "--timeout",
        timeout,
        *extra_args,
    ]
    if not echo:
        cmd.append("-q")
    started = time.monotonic()
    with (run_dir / "warden.log").open("w") as log:
        log.write("$ " + " ".join(cmd) + "\n")
        log.flush()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        assert proc.stdout is not None
        for line in proc.stdout:
            log.write(line)
            if echo:
                sys.stdout.write(line)
        build_exit = proc.wait()
    wall_seconds = time.monotonic() - started

    inspect_json = run_dir / "inspect.json"
    with inspect_json.open("w") as f:
        inspect_exit = subprocess.run(
            [warden, "inspect", str(out), "--json"], stdout=f, stderr=subprocess.DEVNULL, check=False
        ).returncode
    return Run(run_dir, out, inspect_json, build_exit, inspect_exit, wall_seconds)
