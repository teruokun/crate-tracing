"""The analyzer over real BuildWarden runs (tests/fixtures/real/, made with
pack_real.py): the ledger, artifacts and build-phase capture headers exactly
as warden wrote them on 2026-10-29, aarch64, BuildWarden 137feae2f8b0.
"""

import tarfile
from pathlib import Path

import pytest

from crate_tracing import finding, ledger
from crate_tracing.corpus import load_corpus, load_profiles

ROOT = Path(__file__).parents[1]
REAL = Path(__file__).parent / "fixtures" / "real"
WARDEN = {"version": "dev", "revision": "137feae2f8b07e19e21b0893cc4ec9e96f97d42f", "modified": False}
PROFILES = load_profiles(ROOT / "corpus" / "profiles.toml")
CORPUS = {m.id: m for m in load_corpus(ROOT / "corpus" / "poc.toml", PROFILES)}


def real(tmp_path, name: str) -> dict:
    mid, profile = name.split("--")
    with tarfile.open(REAL / f"{name}.tar.gz") as tar:
        tar.extractall(tmp_path, filter="data")
    led = ledger.load(tmp_path / "inspect.json")
    return finding.build(CORPUS[mid], PROFILES[profile], led, tmp_path / "warden-output", WARDEN)


@pytest.mark.parametrize("name", sorted(p.name.removesuffix(".tar.gz") for p in REAL.glob("*.tar.gz")))
def test_real_run_is_measured_and_verified(tmp_path, name):
    f = real(tmp_path, name)
    assert f["outcome"]["status"] == "measured"
    assert f["evidence"]["ledger_valid"] and f["evidence"]["artifacts_verified"]
    assert all(c["lock_checksum_match"] for c in f["crates"])
    assert f["anomalies"] == []
    assert f["matches_expectation"] is True


def test_serde_control_has_no_native_effect(tmp_path):
    f = real(tmp_path, "serde-1.0.229-derive--bare")
    assert f["routes"]["taken"] == ["none"]
    assert set(f["traffic"]) == {"fetch"}
    assert set(f["traffic"]["fetch"]) == {"crates-registry"}


def test_openssl_sys_fails_without_openssl(tmp_path):
    fl = real(tmp_path, "openssl-sys-0.9.117--bare")["outcome"]["failure"]
    assert (fl["package"], fl["version"]) == ("openssl-sys", "0.9.117")
    assert any("Could not find directory of OpenSSL installation" in ln for ln in fl["excerpt"])


def test_openssl_sys_links_the_system_openssl(tmp_path):
    f = real(tmp_path, "openssl-sys-0.9.117--rich")
    libs = {c["name"]: c for c in f["routes"]["C_system"]}
    assert set(libs) == {"ssl", "crypto"}
    assert libs["ssl"]["os_package"] == {"name": "libssl-dev", "version": "3.5.7-1~deb13u3"}
    (pkg,) = [p for p in f["packages"] if p["purl"] == "pkg:cargo/openssl-sys@0.9.117"]
    # Its header probe compiles; that alone is not route B.
    assert pkg["c_compilations"] >= 1
    assert pkg["declared"]["version_number"]
    assert pkg["pkg_config_probes"]


def test_openssl_sys_vendored_names_the_openssl_version(tmp_path):
    f = real(tmp_path, "openssl-sys-0.9.117-vendored--rich")
    (b,) = f["routes"]["B_bundled"]
    assert (b["native"]["name"], b["native"]["version"]) == ("openssl", "3.6.3")
    assert b["native"]["source_crate"] == "pkg:cargo/openssl-src@300.6.1%2B3.6.3"
    assert sorted(b["libs"]) == ["crypto", "ssl"]
    (d,) = f["routes"]["D_tools"]
    assert {"make", "perl"} <= set(d["tools"])


def test_missing_sqlite_is_unresolved(tmp_path):
    f = real(tmp_path, "libsqlite3-sys-0.39.1--bare")
    assert f["outcome"]["built"] is True
    assert [c["name"] for c in f["routes"]["C_unresolved"]] == ["sqlite3"]


def test_ort_downloads_onnx_runtime_and_links_it_from_its_cache(tmp_path):
    f = real(tmp_path, "ort-2.0.0-rc.15--rich")
    (d,) = f["downloads"]
    assert d["url"].startswith("https://cdn.pyke.io/") and d["status"] == 200
    assert d["user_agent"].startswith("ort-sys/2.0.0-rc.15")
    # The archive it linked is the download, not a system library.
    assert {c["name"] for c in f["routes"]["C_system"]} == {"ssl", "crypto"}
    (lib,) = [lib for p in f["packages"] for lib in p["link_libs"] if lib["name"] == "onnxruntime"]
    assert lib["origin"] == "external" and "/ort.pyke.io/" in lib["path"]


def test_v8_downloads_rusty_v8_through_a_redirect(tmp_path):
    f = real(tmp_path, "v8-152.2.0--rich")
    first, second = f["downloads"]
    assert first["status"] == 302 and first["redirect_to"] == second["url"]
    for d in (first, second):
        assert (d["identified"]["name"], d["identified"]["version"]) == ("github.com/denoland/rusty_v8", "152.2.0")
        assert d["user_agent"] == "Python-urllib/3.13"
    assert second["bytes"] > 30 << 20
    (tools,) = f["routes"]["D_tools"]
    assert tools["tools"] == {"python3": 1}
