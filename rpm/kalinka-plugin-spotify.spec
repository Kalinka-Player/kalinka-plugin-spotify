# Built from files staged by scripts/build_rpm.sh, not from a tarball:
#   rpmbuild -bb --define "plugin_version X.Y.Z" --define "stage DIR" \
#     --define "wheel NAME.whl" rpm/kalinka-plugin-spotify.spec
#
# Mirrors the deb. The wheel joins the server's shipped wheels, which the
# server's bootstrap.sh installs into /opt/kalinka/venv whenever it starts,
# and the patched librespot goes where the plugin looks for it by default.

# librespot is built and stripped before packaging; there are no sources
# here to produce debuginfo from.
%global debug_package %{nil}

Name:           kalinka-plugin-spotify
Version:        %{plugin_version}
Release:        1%{?dist}
Summary:        Spotify Connect input for Kalinka Music Player
License:        MIT
URL:            https://github.com/Kalinka-Player/kalinka-plugin-spotify
# kalinka-server bundles the plugin SDK. The server skips the plugin when that
# SDK does not satisfy its REQUIRES_SDK (>=3.5,<4).
Requires:       kalinka-server
Requires:       python3 >= 3.11
Requires:       ca-certificates
# rpmbuild adds the bundled librespot's shared-library requirements.

%description
Server-side Spotify Connect plugin that captures compressed Ogg/Vorbis audio
and serves it to Kalinka's selected renderer. Spotify controls the queue.
Bundles librespot 0.8.0 with the Kalinka pipe bridge. Requires matching
Kalinka server and renderer streaming support.

%install
install -D -m 644 %{stage}/%{wheel} %{buildroot}/opt/kalinka/wheels/%{wheel}
install -D -m 755 %{stage}/librespot \
    %{buildroot}%{_libexecdir}/kalinka-plugin-spotify/librespot
install -D -m 644 %{stage}/LICENSE %{buildroot}%{_licensedir}/%{name}/LICENSE
install -D -m 644 %{stage}/librespot-LICENSE \
    %{buildroot}%{_licensedir}/%{name}/librespot-LICENSE
install -D -m 644 %{stage}/README.md %{buildroot}%{_docdir}/%{name}/README.md

%preun
# Removal only, as in the deb's prerm: take the plugin out of the server's
# venv, where bootstrap.sh would otherwise leave the installed copy.
if [ "$1" -eq 0 ] && [ -x /opt/kalinka/venv/bin/pip ]; then
    /opt/kalinka/venv/bin/pip uninstall -y kalinka-plugin-spotify >/dev/null 2>&1 || :
fi

%postun
if [ "$1" -eq 0 ]; then
    systemctl try-restart kalinka.service >/dev/null 2>&1 || :
fi

%posttrans
# Install or upgrade: the server installs the new wheel when it restarts.
systemctl try-restart kalinka.service >/dev/null 2>&1 || :

%files
/opt/kalinka/wheels/%{wheel}
%dir %{_libexecdir}/kalinka-plugin-spotify
%{_libexecdir}/kalinka-plugin-spotify/librespot
# Own these directories too, or removal leaves them behind empty.
%dir %{_licensedir}/%{name}
%license %{_licensedir}/%{name}/LICENSE
%license %{_licensedir}/%{name}/librespot-LICENSE
%dir %{_docdir}/%{name}
%doc %{_docdir}/%{name}/README.md

%changelog
* Tue Sep 29 2026 Dmitry Savin <envelsavinds@gmail.com> - 0.1.0-1
- Initial RPM package
