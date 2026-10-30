"""crate-tracing command line."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from rich.console import Console
from rich.markup import escape

from . import finding as finding_mod
from . import ledger as ledger_mod
from . import render, runner
from .context import write_context
from .corpus import load_corpus, load_profiles

ROOT = Path.cwd()
# Facts about the run itself, not the ledger: re-analysis keeps them.
RUN_FIELDS = ("warden_exit", "wall_seconds", "output_bytes")
# Evidence promoted next to a finding: enough to re-verify every claim offline.
# Payload captures stay in runs/ (they are whatever the build downloaded).
PROMOTE = ("ledger.zst", "ledger", "ca.cert.pem.zst", "ca.cert.pem", "environment.json")


def _select(args):
    profiles = load_profiles(Path(args.profiles))
    corpus = load_corpus(Path(args.corpus), profiles)
    if args.only:
        wanted = set(args.only)
        unknown = wanted - {m.id for m in corpus}
        if unknown:
            sys.exit(f"unknown measurement id(s): {', '.join(sorted(unknown))}")
        corpus = [m for m in corpus if m.id in wanted]
    pairs = []
    for m in corpus:
        for name in m.profiles:
            if args.profile and name not in args.profile:
                continue
            pairs.append((m, profiles[name]))
    return pairs


def _dump(f: dict) -> str:
    return json.dumps(f, indent=2, sort_keys=False) + "\n"


def _promote(f: dict, run: Path, findings: Path) -> Path:
    mid, prof = f["measurement"]["id"], f["measurement"]["profile"]["name"]
    dest = findings / mid / prof
    if dest.exists():
        shutil.rmtree(dest)
    (dest / "evidence" / "artifacts").mkdir(parents=True)
    (dest / "finding.json").write_text(_dump(f))
    out = run / "warden-output"
    for name in PROMOTE:
        if (out / name).is_file():
            shutil.copyfile(out / name, dest / "evidence" / name)
    if (out / "artifacts").is_dir():
        shutil.copytree(out / "artifacts", dest / "evidence" / "artifacts", dirs_exist_ok=True)
    lock = out / "artifacts" / "ct" / "Cargo.lock"
    if lock.is_file():
        # Shared by every profile: resolution does not depend on the environment.
        shutil.copyfile(lock, findings / mid / "Cargo.lock")
    return dest


def cmd_list(args) -> int:
    console = Console()
    for m, p in _select(args):
        feats = escape("[" + (",".join(m.features) or "default") + "]")
        expect = ",".join(m.expect.get(p.name, ())) or "?"
        console.print(f"[bold]{m.id}[/]  {m.crate} {m.version} {feats}  [cyan]{p.name}[/]  expect={expect}")
    return 0


def cmd_context(args) -> int:
    for m, p in _select(args):
        lock = Path(args.findings) / m.id / "Cargo.lock"
        dest = write_context(Path(args.runs) / m.id / p.name / "context", m, p, lock if args.locked else None)
        print(dest)
    return 0


def cmd_measure(args) -> int:
    console = Console()
    warden = runner.warden_info(args.warden)
    failures = 0
    for m, p in _select(args):
        run_dir = Path(args.runs) / m.id / p.name
        lock = Path(args.findings) / m.id / "Cargo.lock"
        console.rule(f"[bold]{m.id}[/] · {p.name}")
        with console.status(f"warden build ({run_dir})", spinner="dots") if not args.echo else _null():
            r = runner.run(
                m,
                p,
                run_dir,
                warden=args.warden,
                timeout=args.timeout,
                lockfile=lock if args.locked else None,
                echo=args.echo,
            )
        if r.inspect_json.stat().st_size == 0:
            console.print(
                f"[red]warden produced no ledger (build exit {r.build_exit}); see {run_dir / 'warden.log'}[/]"
            )
            failures += 1
            continue
        f = finding_mod.build(m, p, ledger_mod.load(r.inspect_json), r.output, warden)
        f["measurement"]["warden_exit"] = r.build_exit
        f["measurement"]["wall_seconds"] = round(r.wall_seconds, 1)
        f["measurement"]["output_bytes"] = runner.output_bytes(r.output)
        (run_dir / "finding.json").write_text(_dump(f))
        render.finding(f, console)
        if args.promote:
            dest = _promote(f, run_dir, Path(args.findings))
            console.print(f"[dim]promoted to {dest}[/]")
        if f["outcome"]["status"] != "measured":
            failures += 1
    return 1 if failures else 0


def cmd_analyze(args) -> int:
    """Re-analyze existing runs without rebuilding (after changing the analyzer)."""
    console = Console()
    for m, p in _select(args):
        run_dir = Path(args.runs) / m.id / p.name
        inspect = run_dir / "inspect.json"
        if not inspect.is_file():
            console.print(f"[dim]{m.id} · {p.name}: no run[/]")
            continue
        previous = run_dir / "finding.json"
        warden, kept = None, {}
        if previous.is_file():
            # The ledger was made by the warden recorded at measure time.
            before = json.loads(previous.read_text())["measurement"]
            warden = before["tool"].get("warden")
            kept = {k: before[k] for k in RUN_FIELDS if k in before}
        f = finding_mod.build(m, p, ledger_mod.load(inspect), run_dir / "warden-output", warden)
        f["measurement"].update(kept)
        (run_dir / "finding.json").write_text(_dump(f))
        if args.json:
            sys.stdout.write(_dump(f))
        else:
            render.finding(f, console)
        if args.promote:
            _promote(f, run_dir, Path(args.findings))
    return 0


def cmd_show(args) -> int:
    console = Console()
    for path in args.paths:
        f = json.loads(Path(path).read_text())
        if args.json:
            sys.stdout.write(_dump(f))
        else:
            render.finding(f, console)
    return 0


def cmd_summary(args) -> int:
    paths = sorted(Path(args.findings).glob("*/*/finding.json"))
    if not paths:
        print(f"no findings under {args.findings}")
        return 1
    findings = [json.loads(p.read_text()) for p in paths]
    if args.json:
        sys.stdout.write(json.dumps([_summary_row(f) for f in findings], indent=2) + "\n")
    else:
        render.summary(findings)
    return 0


def _summary_row(f: dict) -> dict:
    return {
        "purl": f["subject"]["purl"],
        "features": f["subject"]["features"],
        "measurement": f["measurement"]["id"],
        "profile": f["measurement"]["profile"]["name"],
        "routes": f["routes"]["taken"],
        "matches_expectation": f.get("matches_expectation"),
        "status": f["outcome"]["status"],
    }


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="crate-tracing", description=__doc__)
    ap.add_argument("--corpus", default=str(ROOT / "corpus" / "poc.toml"))
    ap.add_argument("--profiles", default=str(ROOT / "corpus" / "profiles.toml"))
    ap.add_argument("--runs", default=str(ROOT / "runs"), help="working directory for runs (gitignored)")
    ap.add_argument("--findings", default=str(ROOT / "findings"), help="where promoted findings live")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def selection(p):
        p.add_argument("--only", action="append", metavar="ID", help="measurement id (repeatable)")
        p.add_argument("--profile", action="append", metavar="NAME", help="profile name (repeatable)")

    p = sub.add_parser("list", help="list the selected measurements")
    selection(p)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("context", help="write build contexts without running them")
    selection(p)
    p.add_argument("--locked", action="store_true", help="use findings/<id>/Cargo.lock if present")
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("measure", help="run warden build for each measurement and analyze it")
    selection(p)
    p.add_argument("--warden", default="warden")
    p.add_argument("--timeout", default="90m")
    p.add_argument("--locked", action="store_true", help="reuse findings/<id>/Cargo.lock if present (repeatable runs)")
    p.add_argument("--promote", action="store_true", help="copy finding + verifiable evidence into findings/")
    p.add_argument("--echo", action="store_true", help="stream the build console")
    p.set_defaults(func=cmd_measure)

    p = sub.add_parser("analyze", help="re-analyze existing runs")
    selection(p)
    p.add_argument("--json", action="store_true")
    p.add_argument("--promote", action="store_true")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("show", help="render finding.json files")
    p.add_argument("paths", nargs="+")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("summary", help="matrix of all promoted findings")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_summary)

    args = ap.parse_args(argv)
    return args.func(args)
