"""Drive `warden`: build one measurement's context, then inspect the ledger."""

from __future__ import annotations

import shutil
import subprocess
import sys
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


def warden_version(warden: str) -> str | None:
    try:
        out = subprocess.run([warden, "--version"], capture_output=True, text=True, check=False)
    except OSError:
        return None
    return out.stdout.strip().removeprefix("warden version ").strip() or None


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

    inspect_json = run_dir / "inspect.json"
    with inspect_json.open("w") as f:
        inspect_exit = subprocess.run(
            [warden, "inspect", str(out), "--json"], stdout=f, stderr=subprocess.DEVNULL, check=False
        ).returncode
    return Run(run_dir, out, inspect_json, build_exit, inspect_exit)
