"""End-to-end analysis over real guest evidence and relay-shaped ledgers.

The fixtures under tests/fixtures/ are measure.sh's real output from local
builds (tests/fixtures/trim.py); only the ledger around them is synthetic.
"""

import gzip
import io
import json
import tarfile
from pathlib import Path

import pytest
from ledgerkit import LedgerBuilder, measured_run
from rich.console import Console

from crate_tracing import evidence, finding, ledger, render
from crate_tracing.corpus import Measurement, Profile

FIXTURES = Path(__file__).parent / "fixtures"
WARDEN = {"version": "0.0.0-test", "revision": "0" * 40, "modified": False}
BARE = Profile(name="bare", image="rust@sha256:" + "0" * 64)


def analyze(tmp_path, fixture, m, build_requests=(), mutate=None):
    out = tmp_path / "warden-output"
    report = measured_run(out, FIXTURES / fixture, build_requests)
    if mutate:
        mutate(out)
    return finding.build(m, BARE, ledger.parse(report), out, WARDEN)


def test_zstd_sys_compiles_its_bundled_source(tmp_path):
    m = Measurement(id="zstd-sys-2.1.2", crate="zstd-sys", version="2.1.2+zstd.1.5.7", expect={"bare": ("B",)})
    f = analyze(tmp_path, "zstd-sys-2.1.2", m)

    assert f["outcome"]["status"] == "measured"
    assert f["routes"]["taken"] == ["B"]
    assert f["matches_expectation"] is True
    (b,) = f["routes"]["B_bundled"]
    assert b["purl"] == "pkg:cargo/zstd-sys@2.1.2%2Bzstd.1.5.7"
    assert b["libs"] == ["zstd"]
    assert b["c_compilations"] > 30
    assert b["native"] == {
        "name": "zstd",
        "version": "1.5.7",
        "method": "semver-build-metadata",
        "confidence": "high",
    }
    assert all(c["lock_checksum_match"] for c in f["crates"])
    assert f["measurement"]["tool"]["warden"]["revision"] == "0" * 40
    assert f["evidence"]["artifacts_verified"] is True
    assert f["anomalies"] == []


def _syslibs(*rows):
    def write(out):
        (out / "artifacts" / "ct" / "syslibs.tsv").write_text("".join("\t".join(r) + "\n" for r in rows))

    return write


def test_missing_system_library_is_unresolved_not_none(tmp_path):
    # libsqlite3-sys without `bundled` in bare: nothing provides sqlite3, but
    # the probe is a library, so the build still succeeds.
    m = Measurement(id="openssl-sys-0.9.117", crate="openssl-sys", version="0.9.117")
    unit = "openssl-sys-c326141ad8a4002f"
    f = analyze(tmp_path, "openssl-sys-0.9.117", m, mutate=_syslibs((unit, "dylib", "ssl", "", "", "", "")))
    assert f["routes"]["taken"] == ["unresolved"]
    assert [c["name"] for c in f["routes"]["C_unresolved"]] == ["ssl"]
    assert f["routes"]["C_system"] == []


def test_unpackaged_library_outside_the_tree_is_not_route_c(tmp_path):
    # ort-sys links the ONNX Runtime archive it downloaded into ~/.cache.
    m = Measurement(id="openssl-sys-0.9.117", crate="openssl-sys", version="0.9.117")
    path = "/root/.cache/ort.pyke.io/dfbin/libonnxruntime.a"
    row = ("openssl-sys-c326141ad8a4002f", "static", "onnxruntime", path, "", path, "")
    f = analyze(tmp_path, "openssl-sys-0.9.117", m, mutate=_syslibs(row))
    assert "C" not in f["routes"]["taken"]
    (pkg,) = [p for p in f["packages"] if p["purl"].startswith("pkg:cargo/openssl-sys@")]
    assert pkg["link_libs"][0]["origin"] == "external"
    # No download explains it here, so the finding says so.
    assert any("no OS package owns" in a for a in f["anomalies"])


def test_bundled_sqlite(tmp_path):
    m = Measurement(id="libsqlite3-sys-0.39.1-bundled", crate="libsqlite3-sys", version="0.39.1", features=("bundled",))
    f = analyze(tmp_path, "libsqlite3-sys-0.39.1-bundled", m)
    assert f["routes"]["taken"] == ["B"]
    assert f["routes"]["B_bundled"][0]["libs"] == ["sqlite3"]


def test_empty_artifact_verifies_without_a_hash_block(tmp_path):
    out = tmp_path / "warden-output"
    lb = LedgerBuilder(out)
    lb.artifact("ct/syslibs.tsv", b"")
    assert "hash_block" not in lb.records[-1]
    assert evidence.verify(out, ledger.parse(lb.report())) == []
    (out / "artifacts" / "ct" / "syslibs.tsv").write_text("libssl.so.3\n")
    assert evidence.verify(out, ledger.parse(lb.report())) == ["ct/syslibs.tsv"]


def _tgz(root: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        data = b"int main(){}"
        info = tarfile.TarInfo(f"{root}/main.c")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_build_time_download_is_route_a(tmp_path):
    rel = "https://github.com/denoland/rusty_v8/releases/download/v152.2.0/librusty_v8_debug_x86_64-unknown-linux-gnu.a.gz"
    cdn = "https://release-assets.githubusercontent.com/github-production-release-asset/1234/abcd?sp=r"
    archive = "https://example.org/dist/libfoo-2.4.1.tar.gz"
    m = Measurement(id="serde", crate="serde", version="1.0.229")
    f = analyze(
        tmp_path,
        "serde-1.0.229-derive",
        m,
        build_requests=[
            dict(
                url=rel,
                status=302,
                capture=True,
                response_headers={"Location": cdn},
                request_headers={"User-Agent": "ureq/3.1.0"},
            ),
            dict(url=cdn, body=gzip.compress(b"!<arch>\n"), capture=True),
            dict(url=archive, body=_tgz("libfoo-2.4.1"), capture=True),
        ],
    )

    assert "A" in f["routes"]["taken"]
    by_url = {d["url"]: d for d in f["downloads"]}
    assert by_url[rel]["identified"]["name"] == "github.com/denoland/rusty_v8"
    assert by_url[rel]["identified"]["version"] == "152.2.0"
    assert by_url[rel]["redirect_to"] == cdn
    assert by_url[rel]["user_agent"] == "ureq/3.1.0"
    # The redirect target carries the bytes and inherits the identity.
    assert by_url[cdn]["identified"]["version"] == "152.2.0"
    assert by_url[cdn]["identified"]["method"].startswith("redirect-from:")
    assert by_url[cdn]["capture"].startswith("captures/")
    assert by_url[archive]["archive_root"] == "libfoo-2.4.1"
    assert by_url[archive]["identified"]["version"] == "2.4.1"
    # Redirects are evidence, not downloads.
    assert f["routes"]["A_downloads"] == [by_url[cdn]["ledger_seq"], by_url[archive]["ledger_seq"]]
    assert f["matches_expectation"] is None  # no expectation given


def test_failed_build_names_the_crate(tmp_path):
    m = Measurement(id="openssl-sys-0.9.117", crate="openssl-sys", version="0.9.117", expect={"bare": ("fail",)})

    def fail(out):
        ct = out / "artifacts" / "ct"
        (ct / "status.env").write_text("fetch_exit=0\nmetadata_exit=0\nbuild_exit=101\n")
        (ct / "syslibs.tsv").write_text("")
        (ct / "build.log").write_text(
            "error: failed to run custom build command for `openssl-sys v0.9.117`\n"
            "\n"
            "Caused by:\n"
            "  --- stderr\n"
            "  Could not find directory of OpenSSL installation\n"
        )

    f = analyze(tmp_path, "openssl-sys-0.9.117", m, mutate=fail)
    assert f["outcome"]["built"] is False
    assert f["outcome"]["failure"]["package"] == "openssl-sys"
    assert f["outcome"]["failure"]["version"] == "0.9.117"
    assert "Could not find directory of OpenSSL installation" in f["outcome"]["failure"]["excerpt"][0]
    assert f["routes"]["taken"] == ["fail"]
    assert f["matches_expectation"] is True
    # The files changed after the ledger hashed them: the finding says so.
    assert f["evidence"]["artifacts_verified"] is False
    assert any("does not match its ledger hash" in a for a in f["anomalies"])


def test_registry_traffic_during_build_is_an_anomaly(tmp_path):
    m = Measurement(id="serde", crate="serde", version="1.0.229")
    f = analyze(tmp_path, "serde-1.0.229-derive", m, build_requests=[dict(url="https://index.crates.io/se/rd/serde")])
    assert any("registry request during build" in a for a in f["anomalies"])
    assert f["downloads"] == []


def test_missing_markers_make_the_measurement_incomplete(tmp_path):
    lb = LedgerBuilder(tmp_path / "out")
    lb.request("https://example.org/x-1.0.tar.gz", b"x")
    m = Measurement(id="serde", crate="serde", version="1.0.229")
    f = finding.build(m, BARE, ledger.parse(lb.report()), tmp_path / "out")
    assert f["outcome"]["status"] == "incomplete"
    assert f["outcome"]["built"] is None
    assert any("begin-build missing" in a for a in f["anomalies"])


@pytest.mark.parametrize(
    "fixture,crate,version,features",
    [
        ("openssl-sys-0.9.117-vendored", "openssl-sys", "0.9.117", ("vendored",)),
        ("openssl-sys-0.9.117", "openssl-sys", "0.9.117", ()),
    ],
)
def test_render_and_json_round_trip(tmp_path, fixture, crate, version, features):
    m = Measurement(id=fixture, crate=crate, version=version, features=features)
    f = analyze(tmp_path, fixture, m)
    console = Console(record=True, width=140)
    render.finding(json.loads(json.dumps(f)), console)
    render.summary([f], console)
    text = console.export_text()
    assert crate in text
    assert "ledger valid" in text


@pytest.mark.parametrize(
    "fixture,crate,version",
    [
        ("zstd-sys-2.1.2", "zstd-sys", "2.1.2+zstd.1.5.7"),
        ("openssl-sys-0.9.117", "openssl-sys", "0.9.117"),
        ("openssl-sys-0.9.117-vendored", "openssl-sys", "0.9.117"),
        ("libsqlite3-sys-0.39.1-bundled", "libsqlite3-sys", "0.39.1"),
        ("serde-1.0.229-derive", "serde", "1.0.229"),
    ],
)
def test_findings_match_the_schema(tmp_path, fixture, crate, version):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((Path(__file__).parent.parent / "schemas" / "finding-v1.schema.json").read_text())
    m = Measurement(id=fixture, crate=crate, version=version)
    f = analyze(tmp_path, fixture, m, build_requests=[dict(url="https://example.org/a-1.0.tar.gz", body=b"x")])
    jsonschema.validate(json.loads(json.dumps(f)), schema)


def test_unpinned_warden_is_an_anomaly(tmp_path):
    out = tmp_path / "o"
    report = measured_run(out, FIXTURES / "serde-1.0.229-derive")
    m = Measurement(id="serde", crate="serde", version="1.0.229")
    f = finding.build(m, BARE, ledger.parse(report), out, {"version": "dev"})
    assert any("commit unknown" in a for a in f["anomalies"])
    f = finding.build(m, BARE, ledger.parse(report), out, {"revision": "ab" * 20, "modified": True})
    assert any("modified tree" in a for a in f["anomalies"])


def test_terminal_names_crates_and_keeps_other_urls():
    assert render._crate("pkg:cargo/libgit2-sys@0.18.8%2B1.9.7") == "libgit2-sys 0.18.8+1.9.7"
    text = (
        "registry request during build (seq 9): https://static.crates.io/crates/cc/1.2.3/download; "
        "https://static.crates.io/crates/cc/cc-1.2.4.crate; pkg:cargo/ort-sys@2.0.0-rc.15 links x; "
        "https://github.com/denoland/rusty_v8/releases/download/v152.2.0/a.gz"
    )
    assert render._crates_in(text) == (
        "registry request during build (seq 9): cc 1.2.3; cc 1.2.4; ort-sys 2.0.0-rc.15 links x; "
        "https://github.com/denoland/rusty_v8/releases/download/v152.2.0/a.gz"
    )
