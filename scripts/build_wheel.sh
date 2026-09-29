#!/usr/bin/env bash
set -euo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

if (( $# > 1 )); then
    echo "Usage: $0 [output-directory]" >&2
    exit 2
fi

# Honour an existing build environment; do not upgrade its tools implicitly.
python="${PYTHON:-python3}"
"$python" -m build --wheel --outdir "${1:-$root/dist}" "$root"
