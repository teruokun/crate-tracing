# Findings

Promoted by `crate-tracing measure --promote`:

```
findings/<measurement-id>/
├── Cargo.lock                 # pinned resolution, reused by --locked
└── <profile>/
    ├── finding.json           # schemas/finding-v1.schema.json
    └── evidence/
        ├── ledger.zst         # the original signed ledger, byte for byte as warden wrote it
        ├── ca.cert.pem.zst    # the build's ephemeral CA
        ├── environment.json   # the base image manifest the ledger signs
        └── artifacts/ct/...   # the files measure.sh posted (hashed in the ledger)
```

`finding.json` names the BuildWarden that produced the ledger under
`measurement.tool.warden` (version, git `revision`, `modified`, binary
SHA-256). To rebuild that warden, set `BUILDWARDEN_REV` to its `revision`:

```bash
make warden BUILDWARDEN_REV=<revision>
```

To verify a finding's evidence:

```bash
warden inspect findings/<id>/<profile>/evidence
```

Each file under `evidence/artifacts/` must match the SHA-256 of the artifact
record of the same name in that ledger. `crate-tracing analyze` checks this
and reports `evidence.artifacts_verified`.

Payload captures (the bytes that were downloaded) are not kept here; they stay
in the gitignored `runs/` directory.

No findings yet: they come from the test pass (see `HANDOFF.md`).
