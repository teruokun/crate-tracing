import pytest

from crate_tracing import classify, identify, ledger
from crate_tracing.context import cargo_toml, containerfile
from crate_tracing.corpus import Measurement, Profile, cargo_purl


@pytest.mark.parametrize(
    "name,version,links,want",
    [
        ("openssl-src", "300.6.1+3.6.3", None, ("openssl", "3.6.3")),
        ("zstd-sys", "2.1.2+zstd.1.5.7", "zstd", ("zstd", "1.5.7")),
        ("libgit2-sys", "0.18.8+1.9.7", "git2", ("git2", "1.9.7")),
        ("rdkafka-sys", "4.10.0+2.12.1", "rdkafka", ("rdkafka", "2.12.1")),
    ],
)
def test_from_crate(name, version, links, want):
    got = identify.from_crate(name, version, links)
    assert (got["name"], got["version"]) == want
    assert got["method"] == "semver-build-metadata"


def test_from_crate_without_metadata():
    assert identify.from_crate("ring", "0.17.14", "ring_core_0_17_14_") is None


@pytest.mark.parametrize(
    "url,name,version",
    [
        (
            "https://github.com/denoland/rusty_v8/releases/download/v152.2.0/librusty_v8_release_x86_64-unknown-linux-gnu.a.gz",
            "github.com/denoland/rusty_v8",
            "152.2.0",
        ),
        (
            "https://github.com/openssl/openssl/archive/refs/tags/openssl-3.5.0.tar.gz",
            "github.com/openssl/openssl",
            "3.5.0",
        ),
        ("https://www.openssl.org/source/openssl-3.5.0.tar.gz", "openssl", "3.5.0"),
        ("https://example.org/onnxruntime-linux-x64-1.22.0.tgz", "onnxruntime-linux-x64", "1.22.0"),
        ("https://cdn.example.org/pkg/ms@1.22.0/x86_64-unknown-linux-gnu.tgz", "ms", "1.22.0"),
    ],
)
def test_from_url(url, name, version):
    got = identify.from_url(url)
    assert (got["name"], got["version"]) == (name, version)


def test_from_url_unidentified():
    assert identify.from_url("https://example.org/blob/abcdef") is None


def test_split_hash_block():
    block = "aa" * 32 + "bb" * 32 + "cc" * 20 + "dd" * 16
    got = ledger.split_hash_block(block, ["blake2b_256", "sha256", "sha1", "md5"])
    assert got["sha256"] == "bb" * 32 and got["md5"] == "dd" * 16
    # Unknown layout: keep the block whole rather than guess.
    assert ledger.split_hash_block("abcd", ["sha256"]) == {"hash_block": "abcd"}


def test_crate_download_paths():
    r = ledger.Request(
        seq=1, method="GET", url="https://static.crates.io/crates/zstd-sys/2.1.2+zstd.1.5.7/download", phase="fetch"
    )
    assert classify.crate_download(r) == ("zstd-sys", "2.1.2+zstd.1.5.7")
    r.url = "https://static.crates.io/crates/serde/serde-1.0.229.crate"
    assert classify.crate_download(r) == ("serde", "1.0.229")
    assert classify.classify(r) == classify.REGISTRY


def test_purl_encodes_build_metadata():
    assert cargo_purl("zstd-sys", "2.1.2+zstd.1.5.7") == "pkg:cargo/zstd-sys@2.1.2%2Bzstd.1.5.7"


def test_cargo_toml_pins_exactly():
    m = Measurement(id="x", crate="libgit2-sys", version="0.18.8+1.9.7", features=("vendored",), default_features=False)
    toml = cargo_toml(m)
    assert 'libgit2-sys = { version = "=0.18.8", default-features = false, features = ["vendored"] }' in toml


def test_containerfile_installs_only_profile_packages():
    bare = containerfile(Profile(name="bare", image="rust@sha256:" + "0" * 64))
    assert "apt-get" not in bare
    rich = containerfile(Profile(name="rich", image="rust@sha256:" + "0" * 64, apt_packages=("libssl-dev",)))
    assert "apt-get install -y --no-install-recommends libssl-dev" in rich
    assert rich.rstrip().endswith("RUN sh /opt/ct/measure.sh")
