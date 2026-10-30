# Handoff: after the test pass

The first pass (a cloud session with no container runtime) built the tool from
BuildWarden's source alone. The test pass ran it against real `warden build`
runs and published the PoC findings. Read [DESIGN.md](DESIGN.md) first for the
approach.

## Where things stand

- **The whole PoC corpus is measured.** 13 measurements × 3 profiles, all
  `measured`, ledgers valid, artifacts verified, lock checksums matching, no
  anomalies. Every finding is pinned to BuildWarden `137feae2f8b0`, built
  clean (`BUILDWARDEN_REV`). They are in `findings/` with their original
  ledgers. `make summary` shows the matrix.
- **Environment:** aarch64, an Apple silicon Mac with Finch as the container
  runtime (`warden doctor` passes; the container driver, not `vz`). Each
  profile takes 6–9 minutes sequentially. Per-run wall time and output size are in each
  finding and summarized in DESIGN.md (Scaling, item 4).
- **Expectations that were wrong** are corrected in `corpus/poc.toml`. Each
  keeps the original hypothesis in `notes` and says what the evidence showed.
  The surprises: slim's perl lacks FindBin (vendored OpenSSL can't configure),
  slim has no `c++`, `cmake`, python3 or curl, trixie's libgit2 is too old for
  libgit2-sys, aws-lc-sys never runs cmake, ort pulls openssl-sys, and v8
  downloads with python3 rather than a Rust client.
- **`minimal` sits between bare and rich**: build tools, no `-dev` libraries.
  It turns three of bare's six failures into builds (vendored OpenSSL,
  rdkafka-sys, v8). What still fails needs a library: OpenSSL for openssl-sys
  and for ort (its downloader uses native-tls), and curl headers for
  rdkafka-sys's cmake build, through a librdkafka bug (`#cmakedefine01` tested
  with `#ifdef`; details in the corpus notes). 12 of 13 minimal expectations,
  written before measuring, held. Rich is now minimal plus libraries (it
  gained `build-essential` and was re-measured; routes unchanged, but C++
  builds now use `g++` instead of falling back to `clang++`).
- **Tests run over real output.** `tests/fixtures/real/` holds seven packed
  runs (ledger, artifacts, build-phase capture headers), one or more per
  route; `tests/test_real.py` checks them. `tests/fixtures/pack_real.py` packs
  more from `runs/`.

What the real runs changed in the tool:

- `make warden` builds from a checkout in `.tools/buildwarden`. A `go install`
  binary cannot work: warden builds its in-guest helpers from the source tree
  it finds beside its own binary, and only a checkout build has the VCS stamp
  that pins the finding. Use `WARDEN=$PWD/.tools/buildwarden/dist/warden`.
- The relay omits the hash block of an empty artifact; verification accepts
  that for zero-byte records.
- cargo indents `--- stderr` under "Caused by:"; the failure excerpt now finds
  it, and the failure's version no longer keeps cargo's closing backtick.
- Link libraries: `system` now requires an owning OS package. A file outside
  the build tree that no package owns is `external` (ort's downloaded ONNX
  Runtime). A declared library nothing provides is `unresolved`, which is also
  a route outcome when the build succeeds (DESIGN limitation 8).
- `analyze` keeps measure-time facts (`warden_exit`, `wall_seconds`,
  `output_bytes`) when it rebuilds a finding.
- Terminal views name crates as `name version` instead of purls, keep other
  URLs whole, and print downloads and paths as records under the table rows
  rather than in narrow columns. The summary lists identified components below
  the matrix.

## What's next

In rough order of value:

1. **x86_64.** Run `make poc` on an x86_64 Linux host (KVM or Docker). Prebuilt
   downloads are per target, so v8 and ort may differ. Profiles name the
   image by a multi-arch digest, so nothing else changes.
2. **Binary linkage** (DESIGN expansion). It would turn `unresolved` into a
   confirmed link failure and confirm which dynamic libraries really end up
   linked.
3. **Per-crate network attribution** (DESIGN expansion), before measuring
   larger dependency trees.
4. **A pinned rich image.** Rich spends about 20 s and 168 MiB on apt per run.
   Building it once and pinning it by digest makes rich runs cheap enough to
   scale.
5. **Scale the corpus** (DESIGN, Scaling to crates.io).

## Decisions so far

- **Bare is the image exactly as published.** Its failures are findings
  (DESIGN limitation 10); never add packages to it.
- **Debug only** (DESIGN limitation 5). v8's download was 37 MiB, fine.
- **Ledgers are never altered.** Promoted evidence keeps them unmodified.
  Posted artifacts are small so far (the largest promoted measurement is
  2.9 MiB, mostly vendored OpenSSL's build log), so `--promote` copies them
  whole.
- **Signed URLs stay in the record.** v8's GitHub release redirects to a
  `release-assets.githubusercontent.com` URL carrying a short-lived signed
  token (expired an hour after the run). It is in the ledger and the finding
  as received.

## Risks to watch

- **Two versions of one package** in a dependency tree make build-script
  attribution ambiguous. These are flagged `ambiguous_version`.
- **Feature probing that downloads.** Some build scripts check connectivity
  before falling back. These show up as A even when the final route is B or C,
  so read `downloads[].status`.
- **`cargo fetch` without a lock** resolves against the live index. The first
  run's lock is promoted and reused (`--locked`), so later runs are
  reproducible.
- **A missing tool hides an attempted route** (DESIGN limitation 9): read the
  failure excerpt of a bare failure before concluding a crate has no route A.

## Map

| Path | What |
|---|---|
| `corpus/poc.toml`, `corpus/profiles.toml` | what to measure, and where |
| `src/crate_tracing/guest/measure.sh` | runs inside BuildWarden |
| `src/crate_tracing/context.py` | writes the build context |
| `src/crate_tracing/runner.py` | drives `warden build` / `warden inspect` |
| `src/crate_tracing/ledger.py` | inspect JSON → requests, artifacts, phases |
| `src/crate_tracing/captures.py` | matches captured headers and bodies to requests |
| `src/crate_tracing/evidence.py` | reads the posted evidence files, verifies hashes |
| `src/crate_tracing/finding.py` | routes, identification, the finding JSON |
| `src/crate_tracing/identify.py` | version identification heuristics |
| `src/crate_tracing/render.py` | terminal views |
| `schemas/finding-v1.schema.json` | finding format |
| `findings/` | the published findings and their evidence |
| `tests/fixtures/real/` | packed real runs, plus `pack_real.py` to make more |
| `tests/fixtures/<id>/` | guest evidence from local builds, plus `trim.py` |
| `tests/ledgerkit.py` | relay-shaped synthetic ledgers for tests |

To iterate on the analyzer without rebuilding, change it and run
`crate-tracing analyze` over `runs/` (add `--promote` to update `findings/`).
To capture guest evidence without BuildWarden: run `measure.sh` with `CT_OUT`,
`CT_PROBE` and `CT_SHIMS` set and a stub `warden-io` on PATH that copies
`post <file> <name>` into a directory, then use `tests/fixtures/trim.py`.
