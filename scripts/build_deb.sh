#!/usr/bin/env bash
set -euo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
. "$root/scripts/deb_version.sh"

if (( $# != 1 )); then
    echo "Usage: $0 <patched-librespot-executable> (optionally set PYTHON to the build interpreter)" >&2
    exit 2
fi
librespot="$(realpath -- "$1")"
if [[ ! -x "$librespot" ]]; then
    echo "Not an executable: $1" >&2
    exit 1
fi
for tool in "${PYTHON:-python3}" dpkg dpkg-deb dpkg-shlibdeps strip; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "Required build tool not found: $tool" >&2
        exit 1
    fi
done

# The package bundles a native executable, so it is built for the machine it
# is built on: no cross-compilation, and the build distro sets the glibc floor.
arch="$(dpkg --print-architecture)"
if ! "$librespot" --kalinka-capabilities >/dev/null; then
    echo "$librespot is not a Kalinka librespot build for $arch." >&2
    exit 1
fi

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
libexec="$pkgroot/usr/libexec/kalinka-plugin-spotify"
install -d -m 755 "$pkgroot/DEBIAN" "$pkgroot/opt/kalinka/wheels" "$docdir" "$libexec"
install -m 644 "$wheel" "$pkgroot/opt/kalinka/wheels/$wheel_name"
install -m 755 "$librespot" "$libexec/librespot"
strip --strip-unneeded --remove-section=.comment "$libexec/librespot"
install -m 644 "$root/debian/copyright" "$root/debian/README.Debian" "$docdir/"

# dpkg-shlibdeps reads the package list from a debian/control beside it.
install -d "$work/debian"
printf 'Source: kalinka-plugin-spotify\n\nPackage: kalinka-plugin-spotify\nArchitecture: any\n' \
    > "$work/debian/control"
shlibs_depends="$(
    cd "$work"
    dpkg-shlibdeps -O "$libexec/librespot" \
        | sed -n 's/^shlibs:Depends=//p'
)"
if [[ -z "$shlibs_depends" ]]; then
    echo "dpkg-shlibdeps found no shared-library dependencies for librespot." >&2
    exit 1
fi

sed -e "s/@VERSION@/$package_version/g" \
    -e "s/@ARCH@/$arch/g" \
    -e "s/@SHLIBS_DEPENDS@/$shlibs_depends/g" \
    "$root/debian/control.in" > "$pkgroot/DEBIAN/control"
chmod 644 "$pkgroot/DEBIAN/control"
install -m 644 "$root/debian/triggers" "$pkgroot/DEBIAN/triggers"
install -m 755 "$root/debian/prerm" "$pkgroot/DEBIAN/prerm"
(
    cd "$pkgroot"
    find opt usr -type f -print0 | sort -z | xargs -0 md5sum > DEBIAN/md5sums
)
chmod 644 "$pkgroot/DEBIAN/md5sums"

# Build without root and publish only a complete package. Explicit xz keeps
# the archive readable by older Debian/Raspberry Pi OS dpkg versions too.
package="kalinka-plugin-spotify_${package_version}_${arch}.deb"
dpkg-deb --root-owner-group -Zxz --build "$pkgroot" "$work/$package"
install -m 644 "$work/$package" "$root/$package"
printf 'Built %s\n' "$root/$package"
