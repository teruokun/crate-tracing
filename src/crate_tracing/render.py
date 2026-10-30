"""Terminal views of findings (rich). The JSON is the record; this is for people."""

from __future__ import annotations

import re
from urllib.parse import unquote

from rich import box
from rich.columns import Columns
from rich.console import Console, Group
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

ROUTE_STYLE = {
    "A": ("bold red", "build-time download"),
    "B": ("bold yellow", "bundled source compiled"),
    "C": ("bold cyan", "system library linked"),
    "D": ("bold magenta", "build tools run"),
    "unresolved": ("bold blue", "system library missing"),
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


def _crate(purl: str) -> str:
    """`pkg:cargo/name@version` as `name version`; anything else unchanged."""
    if not purl.startswith("pkg:cargo/") or "@" not in purl:
        return purl
    name, version = purl.removeprefix("pkg:cargo/").split("@", 1)
    return f"{unquote(name)} {unquote(version)}"


_PURL = re.compile(r"pkg:cargo/[^\s@]+@[^\s,:;)]+")
# static.crates.io/crates/<name>/<version>/download and .../<name>-<version>.crate
_CRATE_URL = re.compile(
    r"https://static\.crates\.io/crates/(?P<name>[^/\s]+)/"
    r"(?:(?P<version>[^/\s]+)/download|(?P=name)-(?P<file_version>[^/\s]+)\.crate)"
)


def _crates_in(text: str) -> str:
    """Crate purls and crates.io download URLs in free text as `name version`.
    Other URLs are left exactly as they are."""
    text = _CRATE_URL.sub(lambda m: f"{m['name']} {m['version'] or m['file_version']}", text)
    return _PURL.sub(lambda m: _crate(m.group(0)), text)


def _long(text: str, style: str = "", prefix: str = "") -> Padding:
    """A URL or path on its own indented line, folded at the panel's width
    rather than squeezed into a table column. A prefix stays beside it."""
    body = Text(text, style=style, overflow="fold")
    if prefix:
        grid = Table.grid(padding=(0, 1))
        grid.add_column(no_wrap=True, style=style)
        grid.add_column(overflow="fold", ratio=1)
        grid.add_row(prefix, body)
        return Padding(grid, (0, 0, 0, 4))
    return Padding(body, (0, 0, 0, 4))


def _section(title: str) -> Text:
    return Text("\n" + title, style="bold")


def _item(markup: str) -> Padding:
    """A record's summary line; wrapped lines keep its indent."""
    return Padding(Text.from_markup(markup), (0, 0, 0, 2))


def _os_package(lib: dict) -> str:
    osp = lib.get("os_package") or {}
    out = f"{osp.get('name', '?')} [bold]{osp.get('version') or '?'}[/]"
    rt = lib.get("os_runtime_package")
    if rt:
        out += f" [dim](runtime {rt['name']} {rt.get('version') or '?'})[/]"
    return out


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
    head.add_row("crate", f"[bold]{s['name']}[/] {s['version']}")
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
        parts.append(_section("A · downloads during the build"))
        for d in f["downloads"]:
            sha = (d["hashes"].get("sha256") or "")[:16]
            status = d["status"] or "—"
            line = f"[dim]#{d['ledger_seq']}[/]  {status}  {_size(d['bytes'])}  {_ident(d.get('identified'))}"
            if sha:
                line += f"  [dim]sha256 {sha}…[/]"
            parts.append(_item(line))
            parts.append(_long(d["url"]))
            if d.get("redirect_to"):
                parts.append(_long(d["redirect_to"], "dim", prefix="→"))
            if d.get("user_agent"):
                parts.append(_long("user-agent: " + d["user_agent"], "dim"))

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
                native += f"\n[dim]from {_crate(b['native']['source_crate'])}[/]"
            t.add_row(_crate(b["purl"]), native, str(b["c_compilations"]), ", ".join(b.get("libs", [])))
        parts.append(t)

    if r["C_system"]:
        parts.append(_section("C · system libraries linked"))
        for c in r["C_system"]:
            parts.append(_item(f"{_crate(c['purl'])}  [bold]{c['name']}[/] ({c['kind']})  {_os_package(c)}"))
            if c.get("path"):
                parts.append(_long(c["path"], "dim"))

    if r.get("C_unresolved"):
        parts.append(_section("system libraries asked for but missing (a binary would fail to link)"))
        for c in r["C_unresolved"]:
            parts.append(_item(f"{_crate(c['purl'])}  [bold]{c['name']}[/] ({c['kind']})"))

    external = [(p["purl"], lib) for p in f["packages"] for lib in p["link_libs"] if lib["origin"] == "external"]
    if external:
        parts.append(_section("linked from outside the build tree, no OS package"))
        for purl, lib in external:
            parts.append(_item(f"{_crate(purl)}  [bold]{lib['name']}[/] ({lib['kind']})"))
            parts.append(_long(lib["path"], "dim"))
    if isinstance(parts[-1], Padding):
        parts.append(Text(""))  # a record section ends; tables bring their own spacing

    if r["D_tools"]:
        t = Table(title="D · build tools run by build scripts", box=box.SIMPLE_HEAD, title_justify="left", expand=True)
        t.add_column("package")
        t.add_column("tools (invocations)")
        for d in r["D_tools"]:
            t.add_row(_crate(d["purl"]), ", ".join(f"{k}×{v}" for k, v in d["tools"].items()))
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
        parts.append(Text(f"⚠ {_crates_in(a)}", style="yellow"))

    console.print(
        Panel(Group(*parts), title=f"[bold]{m['id']}[/] · {m['profile']['name']}", box=box.ROUNDED, expand=True)
    )


def summary(findings: list[dict], console: Console | None = None) -> None:
    """One row per measurement, one column per profile; what was identified
    follows the matrix as a list, so long OS package versions never wrap."""
    console = console or Console()
    profiles = sorted({f["measurement"]["profile"]["name"] for f in findings})
    rows: dict[str, dict[str, dict]] = {}
    for f in findings:
        rows.setdefault(f["measurement"]["id"], {})[f["measurement"]["profile"]["name"]] = f
    t = Table(title="crate-tracing summary", box=box.ROUNDED, title_justify="left")
    t.add_column("measurement", no_wrap=True)
    for p in profiles:
        t.add_column(p, no_wrap=True)
    identified: dict[str, dict[str, list[str]]] = {}
    for mid in sorted(rows):
        cells = []
        idents: dict[str, list[str]] = {}  # component -> routes/profiles it was seen in
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
                if d.get("identified") and not d["identified"]["method"].startswith("redirect-from:"):
                    idents.setdefault(f"{d['identified']['name']} {d['identified'].get('version', '?')}", []).append(
                        f"A/{p}"
                    )
            for b in f["routes"]["B_bundled"]:
                if b.get("native"):
                    idents.setdefault(f"{b['native']['name']} {b['native'].get('version', '?')}", []).append(f"B/{p}")
            for c in f["routes"]["C_system"]:
                osp = c.get("os_package") or {}
                key = f"{osp.get('name', c['name'])} {osp.get('version') or '?'}"
                if f"C/{p}" not in idents.get(key, []):
                    idents.setdefault(key, []).append(f"C/{p}")
        t.add_row(mid, *cells)
        if idents:
            identified[mid] = idents
    console.print(t)
    console.print(
        Columns(
            [Text.assemble(_chip(k), (f" {label}", "dim")) for k, (_, label) in ROUTE_STYLE.items()], padding=(0, 2)
        )
    )
    console.print(Text("≠ differs from corpus expectation · ! measurement incomplete", style="dim"))
    if identified:
        console.print(Text("\nidentified native components", style="bold"))
        for mid, idents in identified.items():
            console.print(Text(mid))
            for comp, seen in sorted(idents.items()):
                line = Text("    ")
                line.append(comp)
                line.append(f"  ({', '.join(seen)})", style="dim")
                console.print(line, overflow="fold")
