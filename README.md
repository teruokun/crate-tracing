# crate-tracing

When you add a Cargo crate as a dependency, where does its native code come
from, and which version is it? crate-tracing builds the crate inside
[BuildWarden](https://github.com/buildwarden/buildwarden), whose relay records
every byte the build receives to a signed ledger. It then reports the native
code by route, with the ledger as evidence:

| | route | example |
|---|---|---|
| **A** | downloaded at build time | `v8` fetches a prebuilt rusty_v8 from GitHub releases |
| **B** | bundled in a crate and compiled | `openssl-sys` + `vendored` compiles the OpenSSL that `openssl-src` ships |
| **C** | linked from the system | `openssl-sys` links the image's `libssl-dev` |
| **D** | build tools run | cmake, make, perl, protoc invoked by a build script |

Each measurement is a crate at an exact version and feature set, built in two
environment profiles, `bare` and `rich`, because the route usually depends on
what the environment already has.

**Status:** proof of concept. See [DESIGN.md](DESIGN.md) for the approach and
[HANDOFF.md](HANDOFF.md) for what is verified and what comes next.

## Quick start

Needs Python 3.11+, plus `warden` and a container runtime for `measure`.

```bash
make setup test
```

```bash
.venv/bin/crate-tracing list
```

```bash
.venv/bin/crate-tracing measure --only openssl-sys-0.9.117-vendored --profile bare
```

```bash
make poc
```

```bash
make summary
```

## Commands

| command | does |
|---|---|
| `list` | the selected measurements, with each profile's expected routes |
| `context` | writes build contexts without running them (`runs/<id>/<profile>/context`) |
| `measure` | `warden build` + `warden inspect`, then analyze; `--promote` keeps the result in `findings/`, `--locked` reuses the pinned `Cargo.lock`, `--echo` streams the build |
| `analyze` | re-analyzes existing runs without rebuilding; `--json` |
| `show` | renders `finding.json` files; `--json` |
| `summary` | route matrix of all promoted findings; `--json` |

Select with `--only <id>` and `--profile <name>`, both repeatable.

## Output

Each run writes `runs/<id>/<profile>/finding.json`
([schema](schemas/finding-v1.schema.json)), keyed by purl so it can populate
package records elsewhere (e.g. [ecosyste.ms](https://ecosyste.ms)). It also
prints a terminal view:

```
╭─────────────────────────── openssl-sys-0.9.117-vendored · bare ───────────────────────────╮
│ crate     openssl-sys 0.9.117  pkg:cargo/openssl-sys@0.9.117                               │
│ features  vendored                                                                         │
│ result    measured · build succeeded                                                       │
│ routes     B  bundled source compiled   D  build tools run                                 │
│ B · bundled native source compiled in-tree                                                 │
│   package                        native component                       C files  libs      │
│   pkg:cargo/openssl-sys@0.9.117  openssl 3.6.3 (semver-build-metadata)     1216  ssl, crypto│
│                                  from pkg:cargo/openssl-src@300.6.1%2B3.6.3                 │
│ D · build tools run by build scripts                                                       │
│   pkg:cargo/openssl-sys@0.9.117  make×3, perl×1                                            │
│ ledger valid · 8 crates fetched (2 with build scripts) · lock checksums match              │
╰────────────────────────────────────────────────────────────────────────────────────────────╯
```

(Rendered from test-fixture evidence, not yet from a BuildWarden run.)

## Layout

```
corpus/      what to measure (poc.toml) and where (profiles.toml)
src/         the tool; guest/measure.sh runs inside the build
schemas/     finding format
findings/    promoted findings with their verifiable evidence
tests/       analyzer tests over real guest evidence
runs/        per-run working directories (gitignored)
```
