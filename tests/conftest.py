import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import pytest

from kalinka_plugin_spotify.ogg import OggParser


@pytest.fixture
def ogg():
    return (Path(__file__).parent / "fixtures/tone.ogg").read_bytes()


@pytest.fixture
def pages(ogg):
    return OggParser().feed(ogg)
