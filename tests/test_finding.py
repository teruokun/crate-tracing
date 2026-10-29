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

from crate_tracing import finding, ledger, render
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


def test_openssl_sys_links_the_system_openssl(tmp_path):
    m = Measurement(id="openssl-sys-0.9.117", crate="openssl-sys", version="0.9.117")
    f = analyze(tmp_path, "openssl-sys-0.9.117", m)

    assert f["routes"]["taken"] == ["C"]
    libs = {c["name"]: c for c in f["routes"]["C_system"]}
    assert set(libs) == {"ssl", "crypto"}
    assert libs["ssl"]["kind"] == "dylib"
    assert libs["ssl"]["os_package"]["name"] == "libssl-dev"
    assert libs["ssl"]["os_package"]["version"]  # read from dpkg.tsv
    (pkg,) = [p for p in f["packages"] if p["purl"].startswith("pkg:cargo/openssl-sys@")]
    # Its header probe compiles once; that alone is not route B.
    assert pkg["c_compilations"] >= 1
    assert pkg["declared"]["version_number"]
    assert pkg["pkg_config_probes"]


def test_openssl_sys_vendored_names_the_openssl_version(tmp_path):
    m = Measurement(id="openssl-sys-0.9.117-vendored", crate="openssl-sys", version="0.9.117", features=("vendored",))
    f = analyze(tmp_path, "openssl-sys-0.9.117-vendored", m)

    assert f["routes"]["taken"] == ["B", "D"]
    (b,) = f["routes"]["B_bundled"]
    assert b["native"]["name"] == "openssl"
    assert b["native"]["version"] == "3.6.3"
    assert b["native"]["source_crate"] == "pkg:cargo/openssl-src@300.6.1%2B3.6.3"
    assert sorted(b["libs"]) == ["crypto", "ssl"]
    (d,) = f["routes"]["D_tools"]
    assert {"make", "perl"} <= set(d["tools"])


def test_bundled_sqlite(tmp_path):
    m = Measurement(id="libsqlite3-sys-0.39.1-bundled", crate="libsqlite3-sys", version="0.39.1", features=("bundled",))
    f = analyze(tmp_path, "libsqlite3-sys-0.39.1-bundled", m)
    assert f["routes"]["taken"] == ["B"]
    assert f["routes"]["B_bundled"][0]["libs"] == ["sqlite3"]


def test_serde_control_has_no_native_effect(tmp_path):
    m = Measurement(id="serde", crate="serde", version="1.0.229", features=("derive",))
    f = analyze(tmp_path, "serde-1.0.229-derive", m)
    assert f["routes"]["taken"] == ["none"]
    assert f["downloads"] == []
    assert set(f["traffic"]["fetch"]) == {"crates-registry"}
    assert "build" not in f["traffic"]


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
            "--- stderr\n"
            "  Could not find directory of OpenSSL installation\n"
        )

    f = analyze(tmp_path, "openssl-sys-0.9.117", m, mutate=fail)
    assert f["outcome"]["built"] is False
    assert f["outcome"]["failure"]["package"] == "openssl-sys"
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
