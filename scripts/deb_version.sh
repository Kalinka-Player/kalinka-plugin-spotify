# shellcheck shell=bash
# Keep the same development-version ordering as Kalinka's deb build scripts:
# 0.2.0.dev1+g123 -> 0.2.0~dev1+g123, which sorts before 0.2.0 in dpkg.
deb_version() {
    printf '%s\n' "$1" | sed 's/\.dev\([0-9]\)/~dev\1/'
}
