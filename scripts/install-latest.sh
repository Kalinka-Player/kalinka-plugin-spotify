#!/usr/bin/env bash
#
# install-latest.sh — install or upgrade the Spotify Connect plugin from its
# latest GitHub release.
#
# The plugin bundles a native librespot, so each release carries one .deb per
# architecture: amd64 for PCs and arm64 for a 64-bit Raspberry Pi OS. This
# picks the one for this machine, checks it against the release's SHA256SUMS
# and installs it with apt; the server restarts by itself to load it. Run it
# again at any time to upgrade.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Kalinka-Player/kalinka-plugin-spotify/main/scripts/install-latest.sh | sudo bash
#
# Env, passed after sudo, which drops the caller's own:
#   curl -fsSL <url> | sudo GITHUB_TOKEN=... bash
#
#   KALINKA_SPOTIFY_REPO  owner/repo to install from (default: Kalinka-Player/kalinka-plugin-spotify)
#   GITHUB_TOKEN          optional, only to avoid the 60-req/hr anonymous API limit
#
# Everything runs inside main(), called on the last line, so a download cut
# short when piped into bash runs nothing at all.
set -euo pipefail

PACKAGE="kalinka-plugin-spotify"
TMP=""

have() { command -v "$1" >/dev/null 2>&1; }

die() { echo "error: $*" >&2; exit 1; }

cleanup() { if [ -n "$TMP" ]; then rm -rf "$TMP"; fi; }
trap cleanup EXIT

api() {  # api <url>: a GitHub API GET, with the token when there is one
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    # Read from a file descriptor, so the token stays out of the process list.
    curl -fsSL -H @<(printf 'Authorization: Bearer %s\n' "$GITHUB_TOKEN") "$1"
  else
    curl -fsSL "$1"
  fi
}

main() {
  local repo="${KALINKA_SPOTIFY_REPO:-Kalinka-Player/kalinka-plugin-spotify}"
  local sudo="" arch release fields tag version url sums_url name installed sums

  if [ "$(id -u)" -ne 0 ]; then
    have sudo || die "not root and 'sudo' not found — re-run as root"
    sudo="sudo"
  fi
  have apt-get || die "apt-get not found — the plugin installs on a Debian-based system only"
  have python3 || die "python3 is required (the Kalinka server needs it too)"
  have curl || die "curl is required"
  have sha256sum || die "sha256sum is required"

  arch="$(dpkg --print-architecture)"
  case "$arch" in
    amd64|arm64) ;;
    armhf) die "this is a 32-bit system; Spotify Connect needs a 64-bit (arm64) Raspberry Pi OS" ;;
    *) die "unsupported architecture: $arch (need amd64 or arm64)" ;;
  esac

  echo ">> Looking up the latest release of $repo ..."
  release="$(api "https://api.github.com/repos/$repo/releases/latest")" \
    || die "could not query the latest release of $repo"

  # The tag, this architecture's .deb URL and SHA256SUMS' URL, one per line.
  fields="$(python3 -c '
import json, sys
release = json.load(sys.stdin)
assets = {a["name"]: a["browser_download_url"] for a in release.get("assets", [])}
debs = [n for n in assets if n.startswith(sys.argv[1] + "_") and n.endswith("_" + sys.argv[2] + ".deb")]
print(release.get("tag_name", ""))
print(assets[debs[0]] if len(debs) == 1 else "")
print(assets.get("SHA256SUMS", ""))
' "$PACKAGE" "$arch" <<<"$release")" || die "could not read the latest release of $repo"
  tag="$(sed -n 1p <<<"$fields")"
  url="$(sed -n 2p <<<"$fields")"
  sums_url="$(sed -n 3p <<<"$fields")"

  [ -n "$url" ] || die "release $tag has no single ${PACKAGE}_*_${arch}.deb to install"
  [ -n "$sums_url" ] || die "release $tag has no SHA256SUMS to check the download against"
  name="${url##*/}"
  version="${name#"${PACKAGE}_"}"
  version="${version%_"${arch}".deb}"

  # Only a configured package counts: one left unpacked by a failed install is
  # installed again.
  installed="$(dpkg-query -W -f='${db:Status-Status} ${Version}' "$PACKAGE" 2>/dev/null || true)"
  case "$installed" in
    "installed "*) installed="${installed#installed }" ;;
    *) installed="" ;;
  esac
  if [ -n "$installed" ]; then
    if dpkg --compare-versions "$installed" ge "$version"; then
      echo ">> $PACKAGE $installed is installed; the latest release is $version. Nothing to do."
      return 0
    fi
    echo ">> Upgrading $PACKAGE $installed to $version"
  else
    echo ">> Installing $PACKAGE $version ($arch)"
  fi

  # 0755, not mktemp's 0700, so apt's sandbox user can read the .deb.
  TMP="$(mktemp -d)"
  chmod 755 "$TMP"

  echo "   downloading $name"
  curl -fsSL -o "$TMP/$name" "$url"
  sums="$(curl -fsSL "$sums_url")" || die "could not download SHA256SUMS"
  sums="$(awk -v name="$name" '$2 == name' <<<"$sums")"
  [ -n "$sums" ] || die "the release's SHA256SUMS does not list $name"
  (cd "$TMP" && sha256sum --check --strict --quiet <<<"$sums") \
    || die "$name does not match the release's SHA256SUMS"
  echo "   checksum ok"

  # Waits for a dpkg lock held by, say, unattended-upgrades rather than failing.
  if ! $sudo apt-get -o DPkg::Lock::Timeout=300 install -y "$TMP/$name"; then
    echo >&2
    echo "If apt names kalinka-plugin-sdk or kalinka-server above, this release does" >&2
    echo "not fit the Kalinka server installed here. Upgrade Kalinka if it is older" >&2
    echo "than the range apt printed; a newer Kalinka needs a newer release of this plugin." >&2
    return 1
  fi

  echo
  echo ">> Installed $PACKAGE $version. Kalinka restarts to load it;"
  echo "   enable Spotify Connect in Kalinka's settings, then pick the device in the Spotify app."
}

main "$@"
