from pathlib import Path

from kalinka_plugin_spotify import BUNDLED_LIBRESPOT, SpotifyConfig

ROOT = Path(__file__).resolve().parents[1]


def test_default_executable_is_where_the_deb_installs_librespot():
    build_deb = (ROOT / "scripts/build_deb.sh").read_text()
    bundled = Path(BUNDLED_LIBRESPOT)
    assert f'libexec="$pkgroot{bundled.parent}"' in build_deb
    assert f'"$libexec/{bundled.name}"' in build_deb
    assert SpotifyConfig().executable == BUNDLED_LIBRESPOT


def test_default_executable_is_where_the_rpm_installs_librespot():
    spec = (ROOT / "rpm/kalinka-plugin-spotify.spec").read_text()
    bundled = Path(BUNDLED_LIBRESPOT)
    # Fedora's %{_libexecdir} is /usr/libexec, as on Debian.
    assert bundled.parent.parent == Path("/usr/libexec")
    installed = f"%{{_libexecdir}}/{bundled.parent.name}/{bundled.name}"
    assert f"%{{buildroot}}{installed}" in spec.replace("\\\n    ", "")
    assert f"\n{installed}\n" in spec
