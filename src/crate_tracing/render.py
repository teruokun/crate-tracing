"""Terminal views of findings (rich). The JSON is the record; this is for people."""

from __future__ import annotations

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

ROUTE_STYLE = {
    "A": ("bold red", "build-time download"),
    "B": ("bold yellow", "bundled source compiled"),
    "C": ("bold cyan", "system library linked"),
    "D": ("bold magenta", "build tools run"),
    "none": ("bold green", "no native effect"),
    "fail": ("bold white on red", "build failed"),
}


def _chip(route: str) -> Text:
    style = ROUTE_STYLE.get(route, ("bold", route))[0]
    return Text(f" {route} ", style=style if route == "fail" else style + " reverse")


def _route_badges(routes: list[str]) -> Text:
    t = Text()
    for r in routes:
        t.append(_chip(r))
        t.append(f" {ROUTE_STYLE.get(r, ('', r))[1]}  ")
    return t


def _ident(i: dict | None) -> str:
    if not i:
        return "[dim]unidentified[/]"
    name = i.get("name", "?")
    ver = i.get("version", "?")
    return f"{name} [bold]{ver}[/] [dim]({i.get('method')}, {i.get('confidence')})[/]"


def _size(n: int | None) -> str:
    n = n or 0
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def finding(f: dict, console: Console | None = None) -> None:
    console = console or Console()
    s, m, o, r = f["subject"], f["measurement"], f["outcome"], f["routes"]
    feats = ", ".join(s["features"]) or "default"
    if not s["default_features"]:
        feats += " (no default)"

    head = Table.grid(padding=(0, 2))
    head.add_column(style="dim")
    head.add_column()
    head.add_row("crate", f"[bold]{s['name']}[/] {s['version']}  [dim]{s['purl']}[/]")
    head.add_row("features", feats)
    head.add_row("profile", f"{m['profile']['name']}  [dim]{m['arch']} · {m['rustc']}[/]")
    status = "[green]measured[/]" if o["status"] == "measured" else "[red]incomplete[/]"
    build = {True: "[green]succeeded[/]", False: "[red]failed[/]", None: "[dim]not run[/]"}[o["built"]]
    head.add_row("result", f"{status} · build {build}")
    head.add_row("routes", _route_badges(r["taken"]))
    if f.get("matches_expectation") is False:
        head.add_row("expected", Text(" ".join(f["expect"]), style="yellow") + Text("  ≠ measured", style="dim"))

    parts = [head]

    if f["downloads"]:
        t = Table(title="A · downloads during the build", box=box.SIMPLE_HEAD, title_justify="left", expand=True)
        t.add_column("seq", justify="right", style="dim")
        t.add_column("url", overflow="fold")
        t.add_column("status", justify="right")
        t.add_column("size", justify="right")
        t.add_column("identified as")
        t.add_column("sha256", style="dim", overflow="fold")
        for d in f["downloads"]:
            url = d["url"]
            if d.get("redirect_to"):
                url += f"\n[dim]→ {d['redirect_to']}[/]"
            t.add_row(
                str(d["ledger_seq"]),
                url,
                str(d["status"] or "—"),
                _size(d["bytes"]),
                _ident(d.get("identified")),
                (d["hashes"].get("sha256") or "")[:16],
            )
        parts.append(t)

    if r["B_bundled"]:
        t = Table(
            title="B · bundled native source compiled in-tree", box=box.SIMPLE_HEAD, title_justify="left", expand=True
        )
        t.add_column("package")
        t.add_column("native component")
        t.add_column("C files", justify="right")
        t.add_column("static libs")
        for b in r["B_bundled"]:
            native = _ident(b.get("native"))
            if b.get("native", {}).get("source_crate"):
                native += f"\n[dim]from {b['native']['source_crate']}[/]"
            t.add_row(b["purl"], native, str(b["c_compilations"]), ", ".join(b.get("libs", [])))
        parts.append(t)

    if r["C_system"]:
        t = Table(title="C · system libraries linked", box=box.SIMPLE_HEAD, title_justify="left", expand=True)
        t.add_column("package")
        t.add_column("lib")
        t.add_column("kind")
        t.add_column("os package")
        t.add_column("path", style="dim", overflow="fold")
        for c in r["C_system"]:
            osp = c.get("os_package") or {}
            rt = c.get("os_runtime_package")
            osv = f"{osp.get('name', '?')} [bold]{osp.get('version') or '?'}[/]"
            if rt:
                osv += f"\n[dim]{rt['name']} {rt.get('version') or '?'}[/]"
            t.add_row(c["purl"], c["name"], c["kind"], osv, c.get("path", ""))
        parts.append(t)

    if r["D_tools"]:
        t = Table(title="D · build tools run by build scripts", box=box.SIMPLE_HEAD, title_justify="left", expand=True)
        t.add_column("package")
        t.add_column("tools (invocations)")
        for d in r["D_tools"]:
            t.add_row(d["purl"], ", ".join(f"{k}×{v}" for k, v in d["tools"].items()))
        parts.append(t)

    if o.get("failure"):
        fl = o["failure"]
        who = f"{fl.get('package', '?')} {fl.get('version', '')}".strip()
        body = "\n".join(fl.get("excerpt", [])[:12])
        parts.append(
            Panel(Text(body, style="dim"), title=f"build failed in {who}", border_style="red", box=box.ROUNDED)
        )

    traffic = Table(title="traffic by phase", box=box.SIMPLE_HEAD, title_justify="left")
    traffic.add_column("phase")
    traffic.add_column("class")
    traffic.add_column("requests", justify="right")
    traffic.add_column("bytes", justify="right")
    traffic.add_column("hosts", style="dim", overflow="fold")
    for phase in ("setup", "fetch", "build", "collect"):
        for cls, slot in sorted(f["traffic"].get(phase, {}).items()):
            style = "red" if phase == "build" else ""
            traffic.add_row(
                phase, Text(cls, style=style), str(slot["requests"]), _size(slot["bytes"]), " ".join(slot["hosts"])
            )
    parts.append(traffic)

    ev = f["evidence"]
    crates = f["crates"]
    with_bs = sum(1 for c in crates if c.get("has_build_script"))
    lock_ok = all(c["lock_checksum_match"] for c in crates if c["lock_checksum_match"] is not None)
    foot = Text.assemble(
        ("ledger ", "dim"),
        ("valid" if ev["ledger_valid"] else "INVALID", "green" if ev["ledger_valid"] else "bold red"),
        (f" · {ev['ledger_records']} records · ", "dim"),
        (f"{len(crates)} crates fetched ({with_bs} with build scripts)", ""),
        (" · lock checksums ", "dim"),
        ("match" if lock_ok else "MISMATCH", "green" if lock_ok else "bold red"),
        (" · artifacts ", "dim"),
        ("verified" if ev["artifacts_verified"] else "MISMATCH", "green" if ev["artifacts_verified"] else "bold red"),
    )
    parts.append(foot)
    w = m.get("tool", {}).get("warden") or {}
    if w:
        rev = (w.get("revision") or "unknown commit")[:12]
        dirty = " (modified)" if w.get("modified") else ""
        parts.append(Text(f"warden {w.get('version') or '?'} @ {rev}{dirty}", style="dim"))
    for a in f["anomalies"]:
        parts.append(Text(f"⚠ {a}", style="yellow"))

    console.print(
        Panel(Group(*parts), title=f"[bold]{m['id']}[/] · {m['profile']['name']}", box=box.ROUNDED, expand=True)
    )


def summary(findings: list[dict], console: Console | None = None) -> None:
    """One row per measurement, one column per profile."""
    console = console or Console()
    profiles = sorted({f["measurement"]["profile"]["name"] for f in findings})
    rows: dict[str, dict[str, dict]] = {}
    for f in findings:
        rows.setdefault(f["measurement"]["id"], {})[f["measurement"]["profile"]["name"]] = f
    t = Table(title="crate-tracing summary", box=box.ROUNDED, title_justify="left")
    t.add_column("measurement")
    for p in profiles:
        t.add_column(p)
    t.add_column("identified native components", overflow="fold")
    for mid in sorted(rows):
        cells, idents = [], set()
        for p in profiles:
            f = rows[mid].get(p)
            if f is None:
                cells.append(Text("—", style="dim"))
                continue
            cell = Text(" ").join(_chip(r) for r in f["routes"]["taken"]) or Text("?")
            if f.get("matches_expectation") is False:
                cell.append(" ≠", style="yellow")
            if f["outcome"]["status"] != "measured":
                cell.append(" !", style="red")
            cells.append(cell)
            for d in f["downloads"]:
                if d.get("identified"):
                    idents.add(f"{d['identified']['name']} {d['identified'].get('version', '?')} (A)")
            for b in f["routes"]["B_bundled"]:
                if b.get("native"):
                    idents.add(f"{b['native']['name']} {b['native'].get('version', '?')} (B)")
            for c in f["routes"]["C_system"]:
                osp = c.get("os_package") or {}
                idents.add(f"{osp.get('name', c['name'])} {osp.get('version') or '?'} (C/{p})")
        t.add_row(mid, *cells, "\n".join(sorted(idents)))
    console.print(t)
    legend = Text("routes: ", style="dim")
    for k, (_, label) in ROUTE_STYLE.items():
        legend.append(_chip(k))
        legend.append(f" {label}  ", style="dim")
    console.print(legend)
    console.print(Text("≠ differs from corpus expectation · ! measurement incomplete", style="dim"))
