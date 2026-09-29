"""Optional server-side Spotify Connect input for Kalinka."""

import socket
from pathlib import Path
from typing import ClassVar

from kalinka_plugin_sdk import paths
from kalinka_plugin_sdk.datamodel import EmptyList
from kalinka_plugin_sdk.dynamic_fields import DynamicFieldDecl
from kalinka_plugin_sdk.inputmodule import ContentInfo, InputModule
from kalinka_plugin_sdk.module_config import ModuleConfig
from kalinka_plugin_sdk.module_health import ModuleHealthState, ModuleState
from kalinka_plugin_sdk.plugin import InputModulePlugin
from pydantic import Field

from .process import Librespot, forget_credentials
from .service import Service
from .supervisor import Supervisor

BUNDLED_LIBRESPOT = "/usr/libexec/kalinka-plugin-spotify/librespot"


class SpotifyConfig(ModuleConfig):
    __module_icon__: ClassVar[str] = "music_note_outlined"
    __module_icon_color__: ClassVar[str] = "#1DB954"

    name: str = Field(
        default="spotify",
        title="Spotify Connect",
        description="Play from Spotify through the renderer selected in Kalinka.",
        frozen=True,
        exclude=True,
    )
    enabled: bool = Field(
        default=False,
        title="Enable Spotify Connect",
        json_schema_extra={"importance": "simple"},
    )
    device_name: str = Field(
        default_factory=lambda: f"Kalinka ({socket.gethostname()})",
        min_length=1,
        max_length=100,
        title="Spotify device name",
        description="Name shown in Spotify's device list. Choose the output in Kalinka's renderer selector.",
        json_schema_extra={"importance": "simple"},
    )
    unpair: bool = Field(
        default=False,
        title="Unpair Spotify account on next restart",
        json_schema_extra={
            "help": (
                "Forget the saved Spotify sign-in, then wait for a new pairing: "
                "select this device in the Spotify app with a Premium account. "
                "Resets itself once done."
            ),
            # One-shot trigger: the framework resets this (persist-first)
            # before the plugin acts, so it fires at most once per arming.
            "one_shot": True,
            "importance": "simple",
        },
    )
    executable: str = Field(default=BUNDLED_LIBRESPOT, title="librespot executable")
    read_ahead_ms: int = Field(
        default=2000, ge=500, le=5000, title="Read-ahead budget (ms)"
    )
    cache_limit_mib: int = Field(
        default=64, ge=1, le=128, title="Temporary cache limit (MiB)"
    )


class SpotifyInput(InputModule):
    def __init__(self, plugin):
        self.plugin = plugin

    def module_name(self):
        return "spotify"

    def display_name(self):
        return "Spotify Connect"

    async def browse(self, entity_id, offset=0, limit=50, filter=None):
        return EmptyList(offset, limit)

    async def search(self, type, query, offset=0, limit=50):
        return EmptyList(offset, limit)

    async def get_track_info(self, track_ids):
        return []

    async def get_content_info(self, asset_id):
        service = self.plugin.service
        capture = service.capture if service else None
        if capture is None or capture.id != asset_id or capture.retired:
            return None
        return ContentInfo(mime_type="audio/ogg", live=capture, cacheable=False)


class KalinkaPluginSpotify(InputModulePlugin):
    PLUGIN_ID = "spotify"
    REQUIRES_SDK = ">=3.5,<4"
    CONFIG_MODEL = SpotifyConfig
    DYNAMIC_FIELDS: ClassVar[dict[str, DynamicFieldDecl]] = {
        "connect_status": DynamicFieldDecl(
            section_id="", label="Connection status", widget="text"
        ),
    }

    def __init__(self):
        self.service = None
        self.interface = SpotifyInput(self)
        self.enabled = False

    def get_interface(self):
        return self.interface

    async def setup(self, context):
        config = SpotifyConfig(**context.config.model_dump())
        self.enabled = config.enabled
        state_directory = Path(paths.state_dir()) / "spotify"
        if config.unpair:
            # librespot then starts without a sign-in and waits for pairing.
            forget_credentials(state_directory)
        if not config.enabled:
            return
        if context.direct_playback is None:
            raise RuntimeError(
                "Spotify Connect requires Kalinka direct playback and SDK 3.5"
            )
        self.service = Supervisor(
            lambda: Service(
                context.direct_playback,
                Librespot(config.executable, config.device_name, state_directory),
                Path(paths.cache_dir()) / "spotify",
                budget_ms=config.read_ahead_ms,
                max_bytes=config.cache_limit_mib * 1024 * 1024,
            )
        )
        self.service.start()

    async def resolve_dynamic_field(self, path):
        if path == "connect_status":
            return self.service.status if self.service else "Disabled"
        raise KeyError(path)

    async def get_state(self):
        if not self.enabled:
            return ModuleState(state=ModuleHealthState.DISABLED)
        if self.service and self.service.error:
            return ModuleState(
                state=ModuleHealthState.ERROR, message=self.service.error
            )
        if self.service and self.service.output_error:
            return ModuleState(
                state=ModuleHealthState.WARNING, message=self.service.output_error
            )
        return ModuleState(
            state=ModuleHealthState.WARNING
            if self.service and (self.service.closed or self.service.retrying)
            else ModuleHealthState.READY,
            message=await self.resolve_dynamic_field("connect_status"),
        )

    async def shutdown(self):
        if self.service:
            await self.service.stop()
