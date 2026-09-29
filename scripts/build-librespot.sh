#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
work="${1:-$root/build/librespot}"
revision=d36f9f1907e8cc9d68a93f8ebc6b627b1bf7267d
if [[ -e "$work" ]]; then
  echo "Build directory already exists: $work; choose a new directory." >&2
  exit 1
fi
mkdir -p "$work"
git -C "$work" init -q
git -C "$work" remote add origin https://github.com/librespot-org/librespot.git
git -C "$work" fetch --depth 1 origin "$revision"
git -C "$work" checkout --detach FETCH_HEAD
git -C "$work" apply --check "$root/patches/librespot-0.8.0-kalinka.patch"
git -C "$work" apply "$root/patches/librespot-0.8.0-kalinka.patch"
cd "$work"
cargo build --locked --release --no-default-features \
  --features passthrough-decoder,with-libmdns,rustls-tls-native-roots
printf 'Built %s/target/release/librespot\n' "$work"
