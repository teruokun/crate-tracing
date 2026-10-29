"""Assemble a finding: what one measurement shows, with pointers to evidence.

The finding is plain JSON (schemas/finding-v1.schema.json) keyed by purl so it
can populate package records elsewhere (e.g. ecosyste.ms). Each claim points
back at the ledger: a ledger sequence number and content hashes for traffic, an
artifact name for evidence files.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from pathlib import Path

from . import __version__, captures, classify, evidence, identify
from .corpus import Measurement, Profile, cargo_purl
from .ledger import Ledger

SCHEMA = "https://github.com/teruokun/crate-tracing/schemas/finding-v1.schema.json"
PROBE_ROOT = "/work/probe/"

# Libraries every native build links; reported, but not a route-C finding alone.
# Tools that mean a native build ran even when no compiler call reached a shim
# (make and cmake often call the compiler by absolute path).
BUILD_SYSTEMS = {"make", "gmake", "cmake", "ninja", "meson", "autoreconf", "autoconf"}
BASELINE_LIBS = {"c", "m", "dl", "pthread", "rt", "util", "gcc_s", "stdc++", "c++", "resolv"}


def _hashes(h: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in h.items() if k in ("sha256", "blake2b_256", "sha1", "md5")}


def _packages_by_name(meta: dict | None) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for p in (meta or {}).get("packages", []):
        out[p["name"]].append(p)
    return out


def _has_build_script(pkg: dict) -> bool:
    return any("custom-build" in t.get("kind", []) for t in pkg.get("targets", []))


def _src_dependency(pkg: dict, meta: dict | None) -> dict | None:
    """A `*-src` crate this package resolved to (openssl-sys -> openssl-src).

    Read from the resolve graph: declared dependencies include optional ones
    the feature set never enabled.
    """
    by_id = {p["id"]: p for p in (meta or {}).get("packages", [])}
    for node in ((meta or {}).get("resolve") or {}).get("nodes", []):
        if node["id"] != pkg["id"]:
            continue
        for dep in node.get("deps", []):
            target = by_id.get(dep["pkg"])
            if target and target["name"].endswith("-src"):
                return target
    return None


def downloads(ledger: Ledger, output_dir: Path) -> list[dict]:
    """Route A: requests to anything but the registry, made during the build."""
    caps = captures.Captures(output_dir)
    out = []
    for req in ledger.requests:
        cap = caps.take(req)
        if req.phase != "build" or classify.classify(req) != classify.EXTERNAL:
            continue
        body = cap.get("")
        entry = {
            "ledger_seq": req.seq,
            "method": req.method,
            "url": req.url,
            "status": req.status,
            "bytes": req.bytes_in,
            "hashes": _hashes(req.hashes),
            "complete": req.complete,
        }
        location = captures.header(cap.get("response-headers"), "Location")
        if location:
            entry["redirect_to"] = location
        agent = captures.header(cap.get("request-headers"), "User-Agent")
        if agent:
            entry["user_agent"] = agent
        if body is not None:
            entry["capture"] = str(body.relative_to(output_dir))
        ident = identify.from_url(req.url)
        root = captures.archive_root(body)
        if root:
            entry["archive_root"] = root
            ident = ident or identify.from_archive_root(root)
        if ident:
            entry["identified"] = ident
        out.append(entry)

    # A redirect's target carries the bytes but rarely a readable name: let it
    # inherit the identity of the URL that redirected to it.
    by_url = {d["url"]: d for d in out}
    for d in out:
        target = by_url.get(d.get("redirect_to", ""))
        if target is not None and "identified" in d and "identified" not in target:
            target["identified"] = dict(d["identified"], method="redirect-from:" + d["identified"]["method"])
    return out


def crates(ledger: Ledger, ev: evidence.Evidence) -> list[dict]:
    """Every .crate fetched, checked against Cargo.lock's checksum."""
    checksums = evidence.lock_checksums(ev.lock)
    by_name = _packages_by_name(ev.metadata)
    out = []
    for req in ledger.requests:
        nv = classify.crate_download(req)
        if nv is None:
            continue
        name, version = nv
        sha = req.hashes.get("sha256")
        pkg = next((p for p in by_name.get(name, []) if p["version"] == version), None)
        out.append(
            {
                "purl": cargo_purl(name, version),
                "name": name,
                "version": version,
                "phase": req.phase,
                "ledger_seq": req.seq,
                "sha256": sha,
                "lock_checksum_match": (checksums.get((name, version)) == sha) if sha else None,
                "links": pkg.get("links") if pkg else None,
                "has_build_script": _has_build_script(pkg) if pkg else None,
            }
        )
    return out


def packages(ev: evidence.Evidence) -> list[dict]:
    """Per build-script package: what it compiled, linked, ran and declared."""
    by_name = _packages_by_name(ev.metadata)
    tools_by_pkg: dict[str, list[evidence.ToolRun]] = defaultdict(list)
    for t in evidence.tool_runs(ev):
        tools_by_pkg[t.package].append(t)
    links_by_pkg: dict[str, list[evidence.LinkLib]] = defaultdict(list)
    for lib in ev.link_libs:
        links_by_pkg[evidence.unit_package(lib.unit)].append(lib)
    outputs_by_pkg: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for unit, directives in ev.build_outputs.items():
        outputs_by_pkg[evidence.unit_package(unit)].extend(directives)

    out = []
    for name, pkgs in sorted(by_name.items()):
        for pkg in pkgs:
            if not _has_build_script(pkg):
                continue
            # Build-script units and tool logs name the package, not the
            # version: with two versions of one package they are ambiguous.
            ambiguous = len(pkgs) > 1
            runs = [t for t in tools_by_pkg.get(name, []) if not ambiguous or t.version == pkg["version"]]
            compiles = [t for t in runs if t.tool in evidence.COMPILERS and t.mode == "compile"]
            probes = sorted({t.args for t in runs if t.tool in evidence.PROBES})
            tools = Counter(t.tool for t in runs if t.tool not in evidence.COMPILERS | evidence.PROBES)
            links = []
            for lib in links_by_pkg.get(name, []):
                origin = "unresolved"
                if lib.path.startswith(PROBE_ROOT):
                    origin = "in-tree"
                elif lib.path:
                    origin = "system"
                entry = {"name": lib.name, "kind": lib.kind, "origin": origin}
                if lib.path:
                    entry["path"] = lib.path
                if lib.package:
                    entry["os_package"] = {"name": lib.package, "version": ev.dpkg.get(lib.package)}
                if lib.real_package and lib.real_package != lib.package:
                    entry["os_runtime_package"] = {
                        "name": lib.real_package,
                        "version": ev.dpkg.get(lib.real_package),
                    }
                if lib.name in BASELINE_LIBS:
                    entry["baseline"] = True
                links.append(entry)
            # Version-like keys the build script declared to dependents, from
            # `cargo:KEY=VALUE` or `cargo::metadata=KEY=VALUE`.
            declared = {}
            for key, value in outputs_by_pkg.get(name, []):
                if key == "metadata" and "=" in value:
                    key, value = value.split("=", 1)
                if "version" in key.lower():
                    declared[key] = value
            src = _src_dependency(pkg, ev.metadata)
            native = None
            if src is not None:
                native = identify.from_crate(src["name"], src["version"], src.get("links"))
                if native:
                    native["source_crate"] = cargo_purl(src["name"], src["version"])
            native = native or identify.from_crate(name, pkg["version"], pkg.get("links"))
            entry = {
                "purl": cargo_purl(name, pkg["version"]),
                "links": pkg.get("links"),
                "build_script_ran": name in outputs_by_pkg,
                "c_compilations": len(compiles),
                "link_libs": links,
                "tools": dict(sorted(tools.items())),
                "pkg_config_probes": probes,
                "declared": declared,
            }
            if native:
                entry["bundled_native"] = native
            if any(lib["origin"] == "in-tree" for lib in links):
                entry["built_in_tree"] = bool(compiles or native or tools.keys() & BUILD_SYSTEMS)
                if not entry["built_in_tree"]:
                    entry["prebuilt_in_tree"] = True
            if ambiguous:
                entry["ambiguous_version"] = True
            out.append(entry)
    return out


def routes(dl: list[dict], pkgs: list[dict], built: bool | None) -> dict:
    """Collapse the evidence into the four routes, each with its basis."""
    a = [d for d in dl if (d["status"] or 0) < 300 or (d["status"] or 0) >= 400]
    b, c, d_ = [], [], []
    for p in pkgs:
        # B needs a library this package built in-tree. A probe compile alone
        # (openssl-sys checks its headers) is not one, and an in-tree library
        # nothing built was put there whole: prebuilt, so A when downloaded.
        in_tree = [lib for lib in p["link_libs"] if lib["origin"] == "in-tree"]
        if in_tree and p.get("built_in_tree"):
            item = {"purl": p["purl"], "c_compilations": p["c_compilations"], "libs": [lib["name"] for lib in in_tree]}
            if "bundled_native" in p:
                item["native"] = p["bundled_native"]
            b.append(item)
        for lib in p["link_libs"]:
            if lib["origin"] == "system" and not lib.get("baseline"):
                c.append({"purl": p["purl"], **lib})
        if p["tools"]:
            d_.append({"purl": p["purl"], "tools": p["tools"]})
    taken = [r for r, items in (("A", a), ("B", b), ("C", c), ("D", d_)) if items]
    if built is False:
        taken.append("fail")
    elif not taken and built:
        taken = ["none"]
    return {
        "taken": taken,
        "A_downloads": [d["ledger_seq"] for d in a],
        "B_bundled": b,
        "C_system": c,
        "D_tools": d_,
    }


def anomalies(ledger: Ledger, ev: evidence.Evidence) -> list[str]:
    out = []
    if not ledger.valid:
        out.append("ledger failed verification")
    for marker in ("begin-fetch", "begin-build", "begin-collect", "end"):
        if marker not in ledger.markers:
            out.append(f"marker {marker} missing: phases after it are unreliable")
    for req in ledger.requests:
        cls = classify.classify(req)
        if req.phase == "build" and cls == classify.REGISTRY:
            out.append(f"registry request during build (seq {req.seq}): {req.url}")
        if req.phase == "collect" and cls != classify.RELAY:
            out.append(f"network request during collect (seq {req.seq}): {req.url}")
        if not req.complete:
            out.append(f"request never completed (seq {req.seq}): {req.url}")
    for name in ev.mismatched:
        if not name.startswith("ct/marker/"):
            out.append(f"artifact {name} does not match its ledger hash")
    return out


def traffic_summary(ledger: Ledger) -> dict:
    out: dict[str, dict] = {}
    for req in ledger.requests:
        cls = classify.classify(req)
        if cls == classify.RELAY:
            continue
        slot = out.setdefault(req.phase, {}).setdefault(cls, {"requests": 0, "bytes": 0, "hosts": set()})
        slot["requests"] += 1
        slot["bytes"] += req.bytes_in
        slot["hosts"].add(req.host)
    for phase in out.values():
        for slot in phase.values():
            slot["hosts"] = sorted(slot["hosts"])
    return out


def build(
    m: Measurement,
    p: Profile,
    ledger: Ledger,
    output_dir: Path,
    warden: dict | None = None,
) -> dict:
    ev = evidence.load(output_dir, ledger)
    build_exit = ev.status.get("build_exit")
    built = None if build_exit is None or build_exit == -1 else build_exit == 0
    dl = downloads(ledger, output_dir)
    pkgs = packages(ev)
    issues = anomalies(ledger, ev)
    if not (warden or {}).get("revision"):
        issues.append("BuildWarden commit unknown: the measurement cannot be pinned to a warden build")
    elif warden.get("modified"):
        issues.append(f"BuildWarden {warden['revision'][:12]} was built from a modified tree: not reproducible")
    complete = ledger.valid and "end" in ledger.markers

    env = ledger.environment or {}
    finding = {
        "schema": SCHEMA,
        "subject": {
            "purl": m.purl,
            "name": m.crate,
            "version": m.version,
            "features": list(m.features),
            "default_features": m.default_features,
        },
        "measurement": {
            "id": m.id,
            "profile": {
                "name": p.name,
                "image": p.image,
                "apt_packages": list(p.apt_packages),
            },
            "arch": ev.env.get("arch"),
            "host_triple": ev.env.get("host_triple"),
            "rustc": ev.env.get("rustc"),
            "cargo": ev.env.get("cargo"),
            "os": ev.env.get("os.PRETTY_NAME"),
            "measured_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
            "tool": {"crate_tracing": __version__, "warden": warden},
        },
        "outcome": {
            "status": "measured" if complete else "incomplete",
            "fetch_exit": ev.status.get("fetch_exit"),
            "build_exit": build_exit,
            "built": built,
            "failure": evidence.failure(ev),
        },
        "routes": routes(dl, pkgs, built),
        "downloads": dl,
        "packages": pkgs,
        "crates": crates(ledger, ev),
        "traffic": traffic_summary(ledger),
        "evidence": {
            "ledger_valid": ledger.valid,
            "ledger_records": ledger.summary.get("total_records"),
            "environment_type": env.get("type"),
            "environment_digest": env.get("digest"),
            "artifacts_verified": not ev.mismatched,
        },
        "anomalies": issues,
        "expect": list(m.expect.get(p.name, ())),
    }
    exp = set(finding["expect"])
    finding["matches_expectation"] = (exp == set(finding["routes"]["taken"])) if exp else None
    return finding
