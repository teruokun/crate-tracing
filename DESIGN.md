# crate-tracing: design

Status: proof of concept. The analyzer and guest script are built and tested
locally (see [HANDOFF.md](HANDOFF.md)); measurements under BuildWarden are the
next step.

## Goal

For a Cargo crate at an exact version and feature set, produce documented,
verifiable evidence of **where its native code comes from** when it is built as a
dependency, and **which version** of that native code it is. Do it for one
crate at a time, repeatably, so it can later run over most crates on crates.io
that have a `build.rs`.

The motivating example is `openssl-sys`, which is often described as
"downloading OpenSSL when it can't find one". It does not: it links a system
OpenSSL, or, with the `vendored` feature, compiles the OpenSSL source that the
`openssl-src` crate ships inside its `.crate` file. Native code can arrive by
several routes, and which route a crate takes depends on the environment, so the
project measures all of them in more than one environment.

## Routes

| Route | What happens | How it is detected | Version from |
|---|---|---|---|
| **A** build-time download | A build script (or proc macro) fetches something from the network: `v8` gets a prebuilt rusty_v8, `ort` gets ONNX Runtime. | Ledger requests to anything but the registry, made **during the build phase**. | URL (GitHub release tag, `name-1.2.3.tar.gz`, `name@1.2.3`), the URL that redirected to it, or the archive's top-level directory in the captured payload. Every download has its content hashes. |
| **B** bundled source compiled | The native source is inside a crate (`openssl-src`, `zstd-sys`, `libsqlite3-sys` with `bundled`, `ring`) and a build script compiles it. | A static library the package links resolves **inside `target/`**, and the package built it (compiler calls, a build system, or a declared bundled version). | The crate's semver build metadata (`openssl-src 300.6.1+3.6.3` means OpenSSL 3.6.3, `zstd-sys 2.1.2+zstd.1.5.7`), or a version the build script declares (`cargo:version_number=…`). |
| **C** system library linked | The build script finds a library the environment already has, usually through pkg-config. | A `rustc-link-lib` that resolves to a system path owned by a dpkg package. A file outside `target/` that no package owns (a downloaded archive) is `external`, not C. | The owning dpkg package and its version (both the `-dev` and the runtime package). |
| **D** build tools run | A build script runs `cmake`, `make`, `perl`, `protoc`, `go`, `nasm`, `git`, `curl`, ... | Logging shims for each tool on `PATH`, attributed through the `CARGO_PKG_NAME` cargo sets for every build script. | The tool's dpkg package (in `dpkg.tsv`); not reported per invocation yet. |

Plus three outcomes: **none** (the build succeeded and no route was taken,
like serde's compiler probe), **unresolved** (the build succeeded, but a build
script asked to link a system library nothing provides, so a binary would fail
to link; see limitation 8) and **fail** (the build failed in this environment,
with the failing crate and the error excerpt). A build failure is a finding, not a
failed measurement: "`openssl-sys` cannot build without a system OpenSSL" is
the answer for the bare profile.

## What BuildWarden provides, and what it does not

BuildWarden runs the build in an isolated container whose only network path is
a relay that records every request to an Ed25519-chained ledger. For this
project the useful properties are:

- **Completeness.** Every request the build makes goes through the relay, over
  HTTP or HTTPS (the relay terminates TLS with a per-build CA that `warden-io`
  installs as the environment's only root). Nothing can download around it.
- **Order.** Records are strictly ordered and tamper-evident.
- **Content identity.** Each response body is recorded with its size and
  blake2b-256, SHA-256, SHA-1 and MD5.
- **Artifacts.** A file posted with `warden-io post` becomes a signed ledger
  record, its bytes are hashed, and it is saved in the output.
- **Captures.** With `--capture all`, the relay keeps every request's headers
  and body.
- **Environment record.** The first record signs the base image's manifest.

It does **not** provide timestamps on records, which program inside the build
made a request, or any view of what happens without the network (compiling,
linking). The design fills those gaps as follows:

- **Phases come from marker artifacts.** The guest script posts
  `ct/marker/begin-fetch`, `begin-build`, `begin-collect` and `end`. Because
  artifact records land in the same ordered ledger, each request falls into a
  phase by where its open record sits relative to the markers.
- **Non-network evidence becomes artifacts.** Build-script outputs, link
  resolution, tool invocations, dpkg state, `cargo metadata` and `Cargo.lock`
  are posted as artifacts, so they are signed and hashed like the traffic.

## One measurement

A measurement is `(crate, exact version, features, default-features)` from
[`corpus/poc.toml`](corpus/poc.toml), run in one profile from
[`corpus/profiles.toml`](corpus/profiles.toml). Each run is one `warden build`
with the container driver, so it runs on any architecture the base image
supports (the image is pinned by its multi-arch index digest).

### Profiles

- **bare**: the official `rust:<version>-slim-trixie` image, unchanged: a C
  compiler and libc headers, no `-dev` libraries, no pkg-config, no cmake.
  Fallback paths (bundling, downloading) show up here.
- **rich**: bare plus the `-dev` libraries and build tools a typical CI image
  has, so build scripts that prefer a system library find one.

The apt traffic for **rich** is in the ledger's setup phase, so the finding
records exactly what the profile installed.

### Build context (generated by `crate_tracing.context`)

```
Dockerfile          FROM <profile image @digest>; apt for the profile; COPY; RUN measure.sh
probe/Cargo.toml    a lib crate depending on <crate> = "=<version>" with the features
probe/Cargo.lock    when findings/<id>/Cargo.lock exists and --locked is given
ct/measure.sh       the guest script
```

### Guest flow (`src/crate_tracing/guest/measure.sh`)

```
install logging shims for each build tool that exists (cc, cmake, make, perl, ...)
record env.txt (arch, rustc, cargo, os-release) and dpkg.tsv
post ct/marker/begin-fetch
cargo fetch [--locked]                      # the only phase that should hit crates.io
cargo metadata --offline --filter-platform <host>
post ct/marker/begin-build
PATH=shims:$PATH cargo build --locked -vv   # every crate is local: any traffic is a build script's
post ct/marker/begin-collect
for each target/debug/build/<unit>/output:
    post it; resolve each rustc-link-lib against its link-search paths and the
    system lib dirs; dpkg -S the result
post Cargo.lock, env.txt, dpkg.tsv, fetch.log, metadata.json, build.log,
     tools.tsv, syslibs.tsv, status.env
post ct/marker/end; exit 0
```

### Host flow (`crate-tracing measure`)

```
write the build context
warden build <context> -o runs/<id>/<profile>/warden-output --capture all --timeout <t>
warden inspect <output> --json > inspect.json
ledger.parse(inspect.json)        # channels by open_sig; phases by marker position
evidence.load(output)             # artifact files, each checked against its ledger SHA-256
finding.build(...)                # routes, downloads, packages, crates, anomalies
render.finding(...)               # terminal view
write finding.json; with --promote, copy it and the verifiable evidence to findings/
```

## Version identification

Every identification records how it was made and how much to trust it:

| method | confidence | example |
|---|---|---|
| `semver-build-metadata` | high | `openssl-src 300.6.1+3.6.3` means openssl 3.6.3 |
| `github-release-tag` | high | `github.com/denoland/rusty_v8/releases/download/v152.2.0/...` |
| `github-archive-tag` | high/medium | `github.com/x/y/archive/refs/tags/y-1.2.3.tar.gz` |
| `url-filename`, `url-name@version` | medium | `onnxruntime-linux-x64-1.22.0.tgz` |
| `archive-root-dir` | medium | the captured tarball's single top directory is `libfoo-2.4.1/` |
| `redirect-from:<method>` | as its source | a CDN URL that a GitHub release URL redirected to |
| dpkg | exact | `libssl-dev 3.5.1-1` owns `/usr/lib/<triple>/libssl.so` |

The finding also keeps what the build script itself declared (`declared`, e.g.
openssl-sys's `version_number=30600030`), so a consumer can cross-check.

Planned (see Expansions): matching download hashes against known upstream
release hashes.

## Finding format

`finding.json` follows [`schemas/finding-v1.schema.json`](schemas/finding-v1.schema.json).
The key fields:

- `subject.purl`: `pkg:cargo/<name>@<version>` (with `+` percent-encoded).
- `routes.taken`: for example `["B", "D"]`, plus the detail for each route.
- `downloads[]`: URL, status, size, hashes, redirect, User-Agent, identity,
  ledger sequence number.
- `packages[]`: for each build-script package, its compiler calls, link
  libraries and where they resolved, tools, pkg-config probes, declared
  versions, and bundled native identity.
- `crates[]`: every `.crate` fetched, with its SHA-256 checked against
  `Cargo.lock`.
- `evidence`: whether the ledger and artifacts verified, plus the environment
  digest.
- `anomalies[]`: anything that weakens a claim.

Every claim points back to its evidence: traffic by ledger sequence number and
hashes, file-derived claims by an artifact in the same output directory.

### Mapping to ecosyste.ms

Each finding is about one purl, so it can attach to that package's record. A
reasonable projection is one entry per native component:
`{purl, features, profile, route, native: {name, version, method, confidence},
evidence: {url | os_package | source_crate, sha256}}`. `crate-tracing summary
--json` emits the per-measurement route matrix. The exact entity shape should be
agreed with ecosyste.ms before freezing a v2 of the schema.

## Repeatability

- The base image is pinned by digest. The Rust toolchain comes with the image.
- The crate version is exact (`=x.y.z`) and the features are explicit.
- Transitive resolution is pinned. The first run's `Cargo.lock` is promoted to
  `findings/<id>/Cargo.lock`, and `--locked` reuses it, so a re-run fetches the
  same `.crate` hashes. Without it, resolution picks the newest compatible
  versions at the time of the run (the vendored OpenSSL version changes with
  `openssl-src` releases).
- The BuildWarden that measured is pinned. Each finding records the warden
  binary's version, its git commit and dirty flag (from Go's VCS build stamp),
  and the binary's SHA-256 (`measurement.tool.warden`). An unknown commit or a
  dirty build is reported as an anomaly. The Makefile pins the commit used for
  the published findings (`BUILDWARDEN_REV`), and `make warden` builds that
  commit from a checkout. The relay, holder and netns images are fixed by that commit: either
  pinned inside a release binary or built from the same source.
- Build-time downloads are not pinned by anything we control. A re-run that
  downloads different bytes shows up as different hashes.

## Evidence kept in the repository

`findings/<id>/<profile>/` keeps `finding.json` plus `evidence/`: the
original ledger exactly as warden wrote it (`ledger.zst`, never re-encoded),
its CA certificate, `environment.json`, and the posted
artifacts. That is enough to re-verify every claim with `warden inspect
evidence/`, and to check each artifact against its ledger hash. Payload captures
(the downloaded bytes themselves) stay in `runs/`, which is gitignored: they can
be large, and they are third-party content.

## Known limitations

1. **Network attribution is per measurement, not per crate.** A download in
   the build phase is attributed to "the build scripts and proc macros of this
   dependency tree". The User-Agent narrows it: ort-sys sends its own
   (`ort-sys/2.0.0-rc.15 …`), while v8's build script shells out to python3,
   so its requests say `Python-urllib/3.13`. For a crate measured on its own,
   the tree is usually small. Per-crate attribution is an expansion.
2. **Proc macros can reach the network too**, as can `build.rs` of any
   dependency. Both are reported as route A; neither is singled out.
3. **Shims perturb the environment slightly.** Shimmed tools run through a
   `/bin/sh` wrapper. Only tools that exist get shims, so detection of a tool's
   absence (for example via the `which` crate) is unchanged. Compilers called
   by absolute path, as make and cmake often do, bypass the shims; the B rule
   accepts a build system run as evidence of an in-tree build for that reason.
4. **Capture matching is by name and order.** The relay's capture sequence
   counts proxied requests separately from the ledger. Same-named captures are
   matched in ledger order, which is correct unless two identical requests race.
5. **Only debug builds of the host target.** Some crates download different
   artifacts for release builds or other targets (v8 picks a debug or release
   library).
6. **Feature unification is the crate's own.** The probe depends on one crate,
   so its features are exactly those requested plus whatever its own
   dependencies enable.
7. **dpkg-based route C only.** The profiles are Debian. Other distributions
   would need another package-owner query.
8. **The probe is a library, so nothing links.** A build script can ask for a
   system library that does not exist and the build still succeeds:
   libsqlite3-sys in bare declares `sqlite3` without checking for it. Such
   libraries are reported as route `unresolved` (`routes.C_unresolved`), not
   as a failure. Binary linkage (an expansion) would confirm it.
9. **A missing tool hides an attempted route.** Shims exist only for tools
   that exist. v8 in bare tries python3, then curl; neither is in the slim
   image, so the build fails without a single request and with no tool run
   recorded. Only the failure excerpt shows that a download was attempted.
10. **Bare measures the image as much as the crate.** `rust:*-slim` has `cc`
    but no `c++`, `cmake`, full perl (only perl-base), python3 or curl. Most
    bare failures in the PoC are one of these, not the crate's own route.
11. **External libraries are attributed by co-occurrence.** A linked library
    outside the build tree that no OS package owns (ort-sys's ONNX Runtime
    archive in `~/.cache`) is origin `external`. It is explained by a route A
    download in the same build, not matched to it by hash, and is an anomaly
    when there is no download.
12. **One architecture so far.** The PoC findings are aarch64 (an Apple
    silicon Mac running Finch). Prebuilt downloads are per target, so x86_64
    can differ.

## Scaling to crates.io

1. **Candidates.** Use the crates.io database dump, plus the sparse index for
   `links`, latest non-yanked versions. A `links` key is a strong signal of
   native code. Whether a `build.rs` exists has to be confirmed by reading the
   `.crate` (the `build` key in its normalized `Cargo.toml`, or `build.rs` at
   its root). That is a cheap scan outside BuildWarden.
2. **Feature sets.** Run default features first. Then run each feature whose
   name suggests a different route (`vendored`, `bundled`, `static`,
   `download*`, `prebuilt`, `system`, `pkg-config`, `cmake*`). The corpus
   format already pins these explicitly.
3. **Ordering.** Sort by recent downloads, so the crates that matter most are
   measured first.
4. **Throughput.** One measurement is one container plus a relay. Each
   finding records `wall_seconds` and `output_bytes`. In the PoC (26 runs,
   sequential, 8m22s in all on an Apple silicon Mac): bare runs took 7–15 s
   and wrote 4–135 MiB; rich runs took 23–40 s and wrote 316–520 MiB. Rich's
   cost is mostly its setup: about 15 s and 156 MiB of apt packages, captured
   in full. Promoted evidence is far smaller (12 MiB for all 26). Runs are
   independent (BuildWarden stages each in its own directory), so they
   parallelize across cores and machines. Building a rich image once and
   pinning it by digest would remove most of its cost.
5. **Caching.** A shared, read-only crates mirror would cut fetch time, but it
   would remove the registry traffic from the ledger. Prefer ledgered fetches
   until the cost is known.

## Expansions (not in the PoC)

- **Per-crate network attribution.** Segment the build phase per build-script
  package. After `cargo fetch`, run `cargo build -p <pkg>` in dependency order,
  posting a marker between each, so each segment's traffic belongs to one
  package's build script. Cross-check with User-Agent and with captures of the
  build-script binary's environment. Watch for feature-unification rebuilds
  between `-p` runs and the final build.
- **Windows (Hyper-V) and macOS (VZ) guests.** BuildWarden's VM drivers run
  the same `warden-io` flow. The guest script needs PowerShell/zsh ports, and
  route C needs the platform's package-owner query (vcpkg, Homebrew, system
  frameworks). Many crates take entirely different routes there
  (openssl-sys and vcpkg, for example).
- **Release builds and cross targets** as profile dimensions.
- **Binary linkage.** Build a small binary that uses the crate, then run
  `ldd`/`readelf` on it to confirm which dynamic libraries end up linked.
- **Hash matching.** Match download hashes against known upstream artifacts
  (GitHub release assets, distro source tarballs) for exact identity.
- **Output for ecosyste.ms** in their entity format, and CycloneDX/SPDX SBOM
  fragments.

## Non-goals

- Judging whether a route is good or bad. The project records what happens.
- Building crates' own tests or examples.
- Running the crate's code.
