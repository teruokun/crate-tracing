"""Turn a local guest run's posted evidence into a small test fixture.

    python tests/fixtures/trim.py <posted-dir> <probe-dir> <fixture-dir>

<posted-dir> is where a stub `warden-io post` copied measure.sh's artifacts.
Paths are rewritten to the in-guest layout (/work/probe, /usr/local/cargo) and
large files are cut to what the analyzer reads.
"""

import re
import shutil
import sys
from pathlib import Path

KEEP_DPKG = re.compile(r"^(libc6|libgcc-s1|gcc-\d+|libssl|libsqlite3|zlib1g|make|perl|pkg-config|pkgconf)")


def main(posted: Path, probe: Path, dest: Path) -> None:
    src = posted / "ct"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest / "ct", ignore=shutil.ignore_patterns("marker"))
    ct = dest / "ct"

    def rewrite(text: str) -> str:
        text = text.replace(str(probe), "/work/probe")
        # Shim args are cut at 300 characters, so a path can end mid-prefix.
        text = re.sub(re.escape(str(probe.parent)) + r"\S*", "/work/probe/target", text)
        text = re.sub(re.escape(str(probe)[:20]) + r"\S*", "/work/probe/target", text)
        text = re.sub(r"/root/\.cargo", "/usr/local/cargo", text)
        return re.sub(r"/root/\.rustup", "/usr/local/rustup", text)

    for f in ct.rglob("*"):
        if not f.is_file():
            continue
        text = rewrite(f.read_text(errors="replace"))
        if f.name == "build.log":
            text = "\n".join(text.splitlines()[-60:]) + "\n"
        elif f.name in ("output", "stderr"):
            lines = [ln for ln in text.splitlines() if ln.startswith("cargo:")]
            text = "\n".join(lines) + "\n"
        elif f.name == "dpkg.tsv":
            text = "".join(ln + "\n" for ln in text.splitlines() if KEEP_DPKG.match(ln))
        elif f.name == "tools.tsv":
            text = "".join(ln[:240] + "\n" for ln in text.splitlines())
        f.write_text(text)


if __name__ == "__main__":
    main(*(Path(a) for a in sys.argv[1:4]))
