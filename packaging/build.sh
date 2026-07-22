#!/usr/bin/env bash
# build.sh — build reproducible nexus-dashboard packages for every target from
# the SANITIZED public source. The source tree is cloned from the public GitHub
# mirror, which is already scrubbed + gated at publish time (the mirror refresh
# is the sanitization boundary — see packaging/README.md). Building from the
# mirror means every artifact is provably built from public code: no private
# identifier can ever enter a package. Each target builds in a matching
# container so the bundled /opt/nexus-dashboard/venv fits that release's Python.
# Output lands in dist/.
#
#   packaging/build.sh                          # all targets, mirror @ main
#   packaging/build.sh -r v2.2.0                # pin a mirror tag/ref
#   packaging/build.sh -t ubuntu2404            # one target
#   packaging/build.sh --source local           # DEV ONLY: build from THIS
#                                               #   (unsanitized) checkout
#
# Targets: ubuntu2404 (.deb), ubuntu2604 (.deb), rocky9 (.rpm).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MIRROR_URL="https://github.com/brainchillz/NexusDashboard-Modular.git"
REF=main; VERSION=""; RELEASE=1; TARGETS="ubuntu2404,ubuntu2604,rocky9"
OUT="$REPO/dist"; SOURCE=mirror

while [ $# -gt 0 ]; do
    case "$1" in
        -r|--ref)     REF=$2; shift 2 ;;
        -V|--version) VERSION=$2; shift 2 ;;
        -t|--targets) TARGETS=$2; shift 2 ;;
        -o|--out)     OUT=$2; shift 2 ;;
        --source)     SOURCE=$2; shift 2 ;;       # mirror (default) | local
        --mirror-url) MIRROR_URL=$2; shift 2 ;;
        -h|--help)    sed -n '2,19p' "$0"; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
done

command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }

WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT

# --- obtain the SANITIZED source tree ---------------------------------------
# Everything the build consumes — the runtime tree AND install.sh + the
# packaging scripts (units/sudoers/helpers are extracted verbatim from
# install.sh) — comes from ONE tree, so mount it as both /repo and /src.
case "$SOURCE" in
  mirror)
    echo ":: clone sanitized source from $MIRROR_URL @ $REF"
    git clone --quiet --depth 1 --branch "$REF" "$MIRROR_URL" "$WORK/src" 2>/dev/null \
      || git clone --quiet --depth 1 "$MIRROR_URL" "$WORK/src"   # REF may be the default branch
    SRC="$WORK/src"
    SOURCE_DATE_EPOCH=$(git -C "$SRC" log -1 --format=%ct)
    ;;
  local)
    echo "!! --source local: building from THIS checkout — NOT sanitized, DEV ONLY" >&2
    SRC="$REPO"
    SOURCE_DATE_EPOCH=$(git -C "$REPO" log -1 --format=%ct HEAD)
    ;;
  *) echo "unknown --source: $SOURCE (want: mirror|local)" >&2; exit 1 ;;
esac

# The source is sanitized at its boundary — the public-mirror refresh (see the
# install docs). Building from the mirror (the default) is therefore building
# from already-clean source; --source local is explicitly unsanitized dev use,
# warned above. No private-identifier patterns are named here, so this recipe is
# itself safe to ship publicly.
VERSION=${VERSION:-$(grep -oE "APP_VERSION = '[^']+'" "$SRC/nexusdash/core/config.py" \
    | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')}
[ -n "$VERSION" ] || { echo "could not determine VERSION" >&2; exit 1; }

echo "== nexus-dashboard packaging =="
echo "   source=$SOURCE  ref=$REF  version=$VERSION-$RELEASE  epoch=$SOURCE_DATE_EPOCH  targets=$TARGETS"
mkdir -p "$OUT"

declare -A BASE=( [ubuntu2404]=ubuntu:24.04 [ubuntu2604]=ubuntu:26.04 [rocky9]=rockylinux:9 )
declare -A FMT=(  [ubuntu2404]=deb          [ubuntu2604]=deb          [rocky9]=rpm )
declare -A DTAG=( [ubuntu2404]=ubuntu24.04  [ubuntu2604]=ubuntu26.04  [rocky9]=el9 )

fail=0
for t in ${TARGETS//,/ }; do
    base=${BASE[$t]:-}; fmt=${FMT[$t]:-}; dtag=${DTAG[$t]:-}
    [ -n "$base" ] || { echo "unknown target: $t" >&2; fail=1; continue; }
    echo "===== $t  ($base -> .$fmt) ====="
    if docker run --rm \
        -e VERSION="$VERSION" -e RELEASE="$RELEASE" -e DISTTAG="$dtag" \
        -e SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" \
        -v "$SRC:/repo:ro" -v "$SRC:/src:ro" -v "$OUT:/out" \
        "$base" bash "/repo/packaging/$fmt/build-$fmt.sh"; then
        echo ":: $t OK"
    else
        echo "!! $t FAILED" >&2; fail=1
    fi
done

echo; echo "== artifacts in $OUT =="; ls -l "$OUT" | grep -E '\.(deb|rpm)$' || true
exit $fail
