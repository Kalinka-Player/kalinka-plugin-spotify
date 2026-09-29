"""Exercise the settings paths a client reads, saves and reloads."""

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from kalinka_plugin_sdk.module_health import ModuleHealthState
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.config_overrides import apply_overrides_with_prefix, load_overrides
from kalinka_server.config_route import register_config_routes
from kalinka_server.dynamic_field_registry import build_dynamic_field_registry
from kalinka_server.options_registry import OptionsRegistry

from kalinka_plugin_spotify import KalinkaPluginSpotify, SpotifyConfig


def test_settings_schema_paths_can_be_enabled_and_survive_restart(tmp_path):
    config = SpotifyConfig()
    plugin = KalinkaPluginSpotify()
    prepared = SimpleNamespace(
        plugin_context=SimpleNamespace(config=config),
        plugin_instance=plugin,
        plugin_class=KalinkaPluginSpotify,
        health_state=ModuleHealthState.DISABLED,
        error_message=None,
    )
    modules = SimpleNamespace(
        prepared_input_modules={"spotify": prepared}, prepared_devices={}
    )
    app = FastAPI()
    app.state.schema_version = "test"
    app.state.dynamic_field_registry = build_dynamic_field_registry(
        modules.prepared_input_modules, {}
    )
    app.state.dynamic_paths = frozenset(app.state.dynamic_field_registry)
    app.state.options_registry = OptionsRegistry()
    app.state.overrides = {}
    app.state.overrides_file = str(tmp_path / "overrides.json")
    register_config_routes(app, KalinkaConfig(), modules)

    with TestClient(app) as client:
        schema = client.get("/server/config/schema").json()
        page = next(p for p in schema["pages"] if p["id"] == "modules")
        [module] = page["modules"]
        assert module["id"] == "spotify"
        assert module["title"] == "Spotify Connect"
        assert "renderer selected in Kalinka" in module["description"]
        fields = {f["path"]: f for f in module["fields"]}
        values = client.get("/server/config").json()["values"]
        assert fields.keys() <= values.keys()
        assert "input_modules.spotify.target_renderer" not in fields
        assert not values["input_modules.spotify.enabled"]
        assert values["input_modules.spotify.connect_status"] == "Disabled"

        enabled_path = next(p for p in fields if p.endswith(".enabled"))
        device_path = next(p for p in fields if p.endswith(".device_name"))
        payload = {
            "schema_version": "test",
            "changes": {enabled_path: True, device_path: "Living room Spotify"},
        }
        validation = client.post("/server/config/validate", json=payload)
        assert validation.status_code == 200, validation.text
        assert validation.json()["issues"] == []
        saved = client.put("/server/config", json=payload)
        assert saved.status_code == 200, saved.text
        assert config.enabled
        assert config.device_name == "Living room Spotify"

    reloaded = SpotifyConfig()
    apply_overrides_with_prefix(
        reloaded, load_overrides(app.state.overrides_file), "input_modules.spotify."
    )
    assert reloaded.name == "spotify"
    assert reloaded.enabled
    assert reloaded.device_name == "Living room Spotify"
