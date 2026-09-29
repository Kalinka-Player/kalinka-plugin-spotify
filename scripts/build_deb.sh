#!/usr/bin/env bash
set -euo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
. "$root/scripts/deb_version.sh"

if (( $# != 0 )); then
    echo "Usage: $0 (optionally set PYTHON to the build interpreter)" >&2
    exit 2
fi
for tool in "${PYTHON:-python3}" dpkg dpkg-deb; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "Required build tool not found: $tool" >&2
        exit 1
    fi
done

# Build into a fresh directory: a stale dist/ wheel must never determine the
# package version or payload. Preserve existing wheels and source archives.
work="$(mktemp -d "${TMPDIR:-/tmp}/kalinka-spotify-deb.XXXXXXXX")"
trap 'rm -rf -- "$work"' EXIT
"$root/scripts/build_wheel.sh" "$work/wheels"

shopt -s nullglob
wheels=("$work"/wheels/kalinka_plugin_spotify-*-py3-none-any.whl)
if (( ${#wheels[@]} != 1 )); then
    echo "Expected exactly one freshly built, architecture-independent Spotify wheel." >&2
    exit 1
fi
wheel="${wheels[0]}"
wheel_name="$(basename -- "$wheel")"
version="${wheel_name#kalinka_plugin_spotify-}"
version="${version%-py3-none-any.whl}"
package_version="$(deb_version "$version")"
dpkg --validate-version "$package_version"

pkgroot="$work/pkgroot"
docdir="$pkgroot/usr/share/doc/kalinka-plugin-spotify"
install -d -m 755 "$pkgroot/DEBIAN" "$pkgroot/opt/kalinka/wheels" "$docdir"
install -m 644 "$wheel" "$pkgroot/opt/kalinka/wheels/$wheel_name"
install -m 644 "$root/debian/copyright" "$root/debian/README.Debian" "$docdir/"
sed "s/@VERSION@/$package_version/g" "$root/debian/control.in" > "$pkgroot/DEBIAN/control"
chmod 644 "$pkgroot/DEBIAN/control"
install -m 644 "$root/debian/triggers" "$pkgroot/DEBIAN/triggers"
install -m 755 "$root/debian/prerm" "$pkgroot/DEBIAN/prerm"
(
    cd "$pkgroot"
    md5sum "opt/kalinka/wheels/$wheel_name" \
        usr/share/doc/kalinka-plugin-spotify/copyright \
        usr/share/doc/kalinka-plugin-spotify/README.Debian > DEBIAN/md5sums
)
chmod 644 "$pkgroot/DEBIAN/md5sums"

# Build without root and publish only a complete package. Explicit xz keeps
# the archive readable by older Debian/Raspberry Pi OS dpkg versions too.
package="kalinka-plugin-spotify_${package_version}_all.deb"
dpkg-deb --root-owner-group -Zxz --build "$pkgroot" "$work/$package"
install -m 644 "$work/$package" "$root/$package"
printf 'Built %s\n' "$root/$package"
