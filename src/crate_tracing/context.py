"""Generate the build context `warden build` runs for one measurement."""

from __future__ import annotations

import json
import shutil
from importlib import resources
from pathlib import Path

from .corpus import Measurement, Profile


def cargo_toml(m: Measurement) -> str:
    dep = {"version": m.requirement}
    if not m.default_features:
        dep["default-features"] = False
    if m.features:
        dep["features"] = list(m.features)
    inline = ", ".join(f"{k} = {_toml_value(v)}" for k, v in dep.items())
    return (
        "[package]\n"
        'name = "ct-probe"\n'
        'version = "0.0.0"\n'
        'edition = "2021"\n'
        "publish = false\n"
        "\n"
        "[lib]\n"
        'path = "src/lib.rs"\n'
        "\n"
        "[dependencies]\n"
        f"{m.crate} = {{ {inline} }}\n"
    )


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return "[" + ", ".join(json.dumps(x) for x in v) + "]"
    return json.dumps(v)


def containerfile(p: Profile) -> str:
    lines = [
        f"FROM {p.image}",
        "ENV CARGO_TERM_COLOR=never",
        "ENV CARGO_INCREMENTAL=0",
    ]
    if p.apt_packages:
        pkgs = " ".join(p.apt_packages)
        lines += [
            "RUN apt-get update",
            f"RUN DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends {pkgs}",
        ]
    lines += [
        "COPY probe /work/probe",
        "COPY ct /opt/ct",
        "RUN sh /opt/ct/measure.sh",
    ]
    return "\n".join(lines) + "\n"


def write_context(dest: Path, m: Measurement, p: Profile, lockfile: Path | None = None) -> Path:
    """Write the context into `dest` (replacing it) and return it."""
    if dest.exists():
        shutil.rmtree(dest)
    (dest / "probe" / "src").mkdir(parents=True)
    (dest / "ct").mkdir()
    (dest / "Dockerfile").write_text(containerfile(p))
    (dest / "probe" / "Cargo.toml").write_text(cargo_toml(m))
    (dest / "probe" / "src" / "lib.rs").write_text(f"// crate-tracing probe for {m.id}\n")
    if lockfile is not None and lockfile.is_file():
        shutil.copyfile(lockfile, dest / "probe" / "Cargo.lock")
    script = resources.files("crate_tracing").joinpath("guest/measure.sh")
    (dest / "ct" / "measure.sh").write_text(script.read_text())
    (dest / "ct" / "measurement.json").write_text(json.dumps({"id": m.id, "profile": p.name}, indent=2) + "\n")
    return dest
