#!/bin/sh
# crate-tracing guest script: runs inside the BuildWarden build environment.
#
# The ledger records every request in order but carries no timestamps, so the
# script splits it into phases by posting marker artifacts (artifact records
# land in the same ordered ledger):
#
#   setup    everything before ct/marker/begin-fetch (COPY, apt for the profile)
#   fetch    cargo fetch: the only phase that should talk to crates.io
#   build    cargo build --locked: any traffic here comes from build scripts or
#            proc macros, because every crate is already downloaded
#   collect  evidence files posted as artifacts (no network expected)
#
# The script always exits 0 once it reaches the collect phase: a crate that
# fails to build is a finding, not a failed measurement. The cargo exit codes
# are posted in ct/status.env.

set -u

# Overridable only so the script can be exercised outside BuildWarden (tests).
OUT=${CT_OUT:-/tmp/ct-out}
PROBE=${CT_PROBE:-/work/probe}
SHIMS=${CT_SHIMS:-/opt/ct-shims}
mkdir -p "$OUT" "$SHIMS"

say() { printf '\n=== crate-tracing: %s\n' "$*"; }

mark() {
    printf '%s\n' "$1" > "$OUT/marker"
    warden-io post "$OUT/marker" "ct/marker/$1" > /dev/null
}

post() {
    if [ -f "$1" ]; then
        warden-io post "$1" "ct/$2" > /dev/null || say "could not post $2"
    fi
}

# --- tool shims (route D) ---------------------------------------------------
# Each shim logs who ran the tool, then execs the real one. Only tools that
# exist get a shim, so build scripts that probe PATH (the `which` crate) see the
# same tools they would without us. Cargo sets CARGO_PKG_NAME/VERSION for every
# build script and rustc invocation, and child processes inherit it.
for t in cc gcc g++ c++ clang clang++ cmake make gmake ninja meson perl \
         python3 python git curl wget go nasm yasm protoc pkg-config \
         autoreconf autoconf; do
    real=$(command -v "$t" 2> /dev/null) || continue
    # Columns: package, version, tool, mode (compile when a compiler got -c;
    # args are cut, so it is decided here), args.
    cat > "$SHIMS/$t" << EOF
#!/bin/sh
case " \$* " in *" -c "*) mode=compile ;; *) mode=- ;; esac
printf '%s\t%s\t%s\t%s\t%s\n' "\${CARGO_PKG_NAME:-}" "\${CARGO_PKG_VERSION:-}" "$t" "\$mode" "\$(printf '%s' "\$*" | cut -c1-300)" >> $OUT/tools.tsv
exec "$real" "\$@"
EOF
    chmod +x "$SHIMS/$t"
done
: > "$OUT/tools.tsv"

# --- environment ------------------------------------------------------------
{
    echo "arch=$(uname -m)"
    echo "kernel=$(uname -r)"
    echo "rustc=$(rustc --version 2>&1)"
    echo "cargo=$(cargo --version 2>&1)"
    echo "host_triple=$(rustc -vV 2>/dev/null | sed -n 's/^host: //p')"
    echo "shims=$(ls "$SHIMS" | tr '\n' ' ')"
    sed 's/^/os./' /etc/os-release 2> /dev/null
} > "$OUT/env.txt"
dpkg-query -W -f='${Package}\t${Version}\t${Architecture}\n' > "$OUT/dpkg.tsv" 2> /dev/null || :

# --- fetch ------------------------------------------------------------------
cd "$PROBE" || exit 1
say "fetch"
mark begin-fetch
if [ -f Cargo.lock ]; then
    cargo fetch --locked > "$OUT/fetch.log" 2>&1
else
    cargo fetch > "$OUT/fetch.log" 2>&1
fi
fetch_exit=$?
host=$(rustc -vV | sed -n 's/^host: //p')
cargo metadata --format-version 1 --locked --offline --filter-platform "$host" \
    > "$OUT/metadata.json" 2>> "$OUT/fetch.log"
metadata_exit=$?
tail -n 20 "$OUT/fetch.log"

# --- build ------------------------------------------------------------------
say "build"
mark begin-build
build_exit=-1
if [ "$fetch_exit" -eq 0 ]; then
    PATH="$SHIMS:$PATH" cargo build --locked -vv > "$OUT/build.log" 2>&1
    build_exit=$?
    tail -n 40 "$OUT/build.log"
fi

# --- collect ----------------------------------------------------------------
say "collect (cargo build exit $build_exit)"
mark begin-collect

# Link directives from every build script, and where each named library
# resolves: inside target/ (compiled from crate contents or downloaded) or a
# system path owned by a dpkg package.
multiarch=$(gcc -dumpmachine 2> /dev/null || echo unknown)
: > "$OUT/syslibs.tsv"
for f in target/debug/build/*/output; do
    [ -f "$f" ] || continue
    dir=$(dirname "$f")
    unit=$(basename "$dir")
    post "$f" "buildscript/$unit/output"
    [ -s "$dir/stderr" ] && post "$dir/stderr" "buildscript/$unit/stderr"
    search=$(sed -n 's/^cargo::\{0,1\}rustc-link-search=//p' "$f" | sed 's/^[a-z]*=//' | tr '\n' ' ')
    sed -n 's/^cargo::\{0,1\}rustc-link-lib=//p' "$f" | while read -r spec; do
        case "$spec" in
            *=*) kind=${spec%%=*}; name=${spec#*=} ;;
            *) kind=dylib; name=$spec ;;
        esac
        kind=${kind%%:*}
        name=${name%%:*}
        found=""
        for d in $search /usr/local/lib "/usr/lib/$multiarch" /usr/lib "/lib/$multiarch" /lib; do
            for c in "$d/lib$name.so" "$d/lib$name.a"; do
                if [ -e "$c" ]; then found=$c; break 2; fi
            done
        done
        real=""; pkg=""; realpkg=""
        if [ -n "$found" ]; then
            real=$(readlink -f "$found")
            pkg=$(dpkg -S "$found" 2> /dev/null | head -n1 | cut -d: -f1)
            realpkg=$(dpkg -S "$real" 2> /dev/null | head -n1 | cut -d: -f1)
        fi
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$unit" "$kind" "$name" "$found" "$pkg" "$real" "$realpkg" >> "$OUT/syslibs.tsv"
    done
done

{
    echo "fetch_exit=$fetch_exit"
    echo "metadata_exit=$metadata_exit"
    echo "build_exit=$build_exit"
} > "$OUT/status.env"

post Cargo.lock Cargo.lock
post "$OUT/env.txt" env.txt
post "$OUT/dpkg.tsv" dpkg.tsv
post "$OUT/fetch.log" fetch.log
post "$OUT/metadata.json" metadata.json
post "$OUT/build.log" build.log
post "$OUT/tools.tsv" tools.tsv
post "$OUT/syslibs.tsv" syslibs.tsv
post "$OUT/status.env" status.env
mark end
say "done"
exit 0
