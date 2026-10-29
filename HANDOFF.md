# Handoff: first pass → test pass

This is where the first pass (a cloud session with no container runtime)
stopped, and how the next session, on a machine where `warden build` works,
picks it up. Read [DESIGN.md](DESIGN.md) first for the approach.

## Where things stand

**Built and tested here:**

- The corpus (13 measurements, pinned versions and features, a route
  hypothesis per profile) and the two profiles. The image is pinned to the
  multi-arch digest of `rust:1.98.1-slim-trixie`.
- Build context generation (`crate-tracing context`).
- The guest script `measure.sh`. It was exercised **outside BuildWarden**, with
  a stub `warden-io` that copies posted files, against real cargo builds of
  zstd-sys, openssl-sys (default and `vendored`), libsqlite3-sys `bundled` and
  serde. Their real evidence, trimmed, is in `tests/fixtures/`.
- The analyzer. It parses ledgers, assigns phases by marker, matches captures,
  verifies artifacts against ledger hashes, detects routes A–D, identifies
  versions, and checks fetched crates against `Cargo.lock`. Tests run it over
  the real fixtures wrapped in relay-shaped synthetic ledgers
  (`tests/ledgerkit.py`).
- The terminal views (`show`, `summary`), the JSON output, and its JSON Schema
  (validated in tests).
- `make test` (32 tests) and `make lint` pass.

**Not done, because this machine had no Docker daemon or KVM:**

- No `warden build` has run. Every assumption about the real ledger shape comes
  from reading BuildWarden's source (`relay/relay.go`, `cmd/warden/inspect_impl.go`,
  `relay/capture.go`), not from an observed run.
- `findings/` is empty. The example findings come from the test pass.

The local evidence differs from what a real run will show: the guest ran on
Ubuntu with libssl-dev installed, so "openssl-sys default" linked system
OpenSSL even in a "bare" run. The tests use that evidence for route C, which is
fine; the bare profile in a real run should fail instead.

## Test pass: steps

Prerequisites: `warden` on PATH, a container runtime `warden build` already
works with, Python 3.11+.

```bash
make setup test lint
```

### 1. Smoke test: the control

```bash
.venv/bin/crate-tracing measure --only serde-1.0.229-derive --profile bare --echo
```

Check, in `runs/serde-1.0.229-derive/bare/`:

- [ ] `warden.log` shows the build ran, and `inspect.json` is non-empty.
- [ ] The finding renders, and status is `measured`: the four markers were
      found, which means artifact records carry `metadata.name` exactly as
      `ledger.py` expects.
- [ ] `traffic` shows only `crates-registry` in `fetch` and no `build` phase
      traffic, and `routes.taken == ["none"]`.
- [ ] `crates[].lock_checksum_match` is all true. This confirms that
      static.crates.io bodies are hashed as the `.crate` bytes and that the
      hash-block layout splits as `blake2b_256|sha256|sha1|md5`.
- [ ] `evidence.artifacts_verified` is true. This confirms that warden stores
      artifacts at `warden-output/artifacts/<name>`.
- [ ] `captures/` exists, and capture names follow `NNN-METHOD-host-path`.

If any of these fail, fix the parser against the real `inspect.json` first:
`ledger.py` and `captures.py` are where the assumptions live. Then turn the
real `inspect.json` into a test fixture.

### 2. One per route

```bash
.venv/bin/crate-tracing measure --only openssl-sys-0.9.117 --only openssl-sys-0.9.117-vendored --echo
```

```bash
.venv/bin/crate-tracing measure --only v8-152.2.0 --profile bare --echo
```

- [ ] openssl-sys default: **bare fails** with a failure excerpt naming
      openssl-sys; **rich** shows route C with libssl-dev's version.
- [ ] Vendored: route B names OpenSSL from `openssl-src`'s build metadata,
      and route D shows make and perl. In **bare** this may fail if the slim
      image lacks `make` or perl modules. If so, that is a real finding: record
      it, and don't add packages to bare.
- [ ] v8: route A, with the rusty_v8 GitHub release URL identified at
      `152.2.0`, its redirect target carrying the bytes, and a `ureq` (or
      similar) User-Agent. The debug archive is large; check
      `--timeout` and disk space.
- [ ] The rich profile's `apt-get install` succeeds. All its package names
      exist on trixie as far as I know, but none was installed in a real
      run. Fix the list in `corpus/profiles.toml` if not.

### 3. The full corpus

```bash
make poc
```

```bash
make summary
```

- [ ] Every run is `measured`. For each `≠` (finding differs from `expect`),
      decide whether the expectation or the analyzer is wrong. Update `expect`
      only when the evidence shows the hypothesis was wrong, and say why in the
      entry's `notes`.
- [ ] Record wall time and output size per run (add to the finding if useful)
      for the throughput estimate in DESIGN.md.
- [ ] Commit `findings/` (finding + evidence + `Cargo.lock` per measurement).

### 4. Fold the results back

- [ ] Turn one real output per route into fixtures, and replace the synthetic
      ledgers in `tests/test_finding.py` where a real one exists.
- [ ] Update DESIGN.md's limitations with anything the real runs showed.
- [ ] Remove the "Not done" section above, and update "Where things stand".

## Decisions for the test pass

- **Profile contents.** Bare is the image exactly as published. If it cannot
  even build `cc`-based crates, keep it anyway: it is the measurement.
- **Debug vs release.** Debug only for now (DESIGN limitation 5). If v8's debug
  download is unreasonable, consider a `release` profile dimension rather than
  switching the default.
- **Evidence size.** If committed evidence grows large (build logs from `-vv`),
  compress `artifacts/ct/build.log` in `--promote`, or keep only the ledger.
  The artifact hashes stay in the ledger either way.

## Risks to watch

- **Two versions of one package** in a dependency tree make build-script
  attribution ambiguous. These are flagged `ambiguous_version`.
- **Feature probing that downloads.** Some build scripts check connectivity
  before falling back. These show up as A even when the final route is B or C,
  so read `downloads[].status`.
- **`cargo fetch` without a lock** resolves against the live index. The first
  run's lock is promoted and reused (`--locked`), so later runs are
  reproducible.

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
| `tests/fixtures/` | real guest evidence, plus `trim.py` to make more |
| `tests/ledgerkit.py` | relay-shaped synthetic ledgers for tests |

To capture new guest evidence without BuildWarden, for a quick analyzer
iteration: run `measure.sh` with `CT_OUT`, `CT_PROBE` and `CT_SHIMS` set, a
stub `warden-io` on PATH that copies `post <file> <name>` into a directory,
then use `tests/fixtures/trim.py`.
