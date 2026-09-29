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
for tool in "${PYTHON:-python3}" rpm rpmbuild strip; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "Required build tool not found: $tool" >&2
        exit 1
    fi
done

# As with the deb, the package bundles a native executable built on this
# machine; the build distro sets the glibc floor and the dist tag.
arch="$(rpm --eval '%{_arch}')"
if ! "$librespot" --kalinka-capabilities >/dev/null; then
    echo "$librespot is not a Kalinka librespot build for $arch." >&2
    exit 1
fi

# Build into a fresh directory: a stale dist/ wheel must never determine the
# package version or payload.
work="$(mktemp -d "${TMPDIR:-/tmp}/kalinka-spotify-rpm.XXXXXXXX")"
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
# RPM also sorts ~ before a release, so dev builds use the deb's mapping.
package_version="$(deb_version "$version")"

stage="$work/stage"
install -d -m 755 "$stage"
install -m 644 "$wheel" "$stage/$wheel_name"
install -m 755 "$librespot" "$stage/librespot"
strip --strip-unneeded --remove-section=.comment "$stage/librespot"
install -m 644 "$root/LICENSE" "$root/README.md" "$stage/"
install -m 644 "$root/docs/librespot-LICENSE" "$stage/librespot-LICENSE"

rpmbuild -bb --quiet \
    --define "_topdir $work/rpmbuild" \
    --define "plugin_version $package_version" \
    --define "stage $stage" \
    --define "wheel $wheel_name" \
    "$root/rpm/kalinka-plugin-spotify.spec"

rpms=("$work"/rpmbuild/RPMS/"$arch"/kalinka-plugin-spotify-*."$arch".rpm)
if (( ${#rpms[@]} != 1 )); then
    echo "rpmbuild did not produce exactly one $arch package." >&2
    exit 1
fi
install -m 644 "${rpms[0]}" "$root/"
printf 'Built %s\n' "$root/$(basename -- "${rpms[0]}")"
