"""Pack a real BuildWarden run into a test fixture.

    python tests/fixtures/pack_real.py runs/<id>/<profile> [...]

Writes tests/fixtures/real/<id>--<profile>.tar.gz holding `inspect.json` (the
verified ledger), the posted artifacts byte for byte (so they still match the
ledger's hashes), and the captured headers of build-phase requests. Payload
bodies are left out: they are whatever the build downloaded.
"""

import io
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from crate_tracing import ledger  # noqa: E402
from crate_tracing.captures import Captures  # noqa: E402

DEST = Path(__file__).parent / "real"


def pack(run: Path) -> Path:
    out = run / "warden-output"
    inspect = run / "inspect.json"
    caps = Captures(out)
    keep: list[Path] = []
    for req in ledger.load(inspect).requests:
        files = caps.take(req)  # in ledger order, as the analyzer matches them
        if req.phase == "build":
            keep += [p for suffix, p in files.items() if suffix]

    DEST.mkdir(exist_ok=True)
    dest = DEST / f"{run.parent.name}--{run.name}.tar.gz"
    with tarfile.open(dest, "w:gz", compresslevel=9) as tar:

        def add(path: Path, name: str) -> None:
            info = tarfile.TarInfo(name)
            data = path.read_bytes()
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        add(inspect, "inspect.json")
        for f in sorted((out / "artifacts").rglob("*")):
            if f.is_file():
                add(f, "warden-output/" + f.relative_to(out).as_posix())
        for f in keep:
            add(f, "warden-output/captures/" + f.name)
    return dest


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        p = pack(Path(arg))
        print(f"{p}  {p.stat().st_size // 1024} KiB")
