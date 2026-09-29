#!/usr/bin/env python3
"""Check the compiled decoder and pipe output offline, including a seek."""

import json
import os
import select
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from kalinka_plugin_spotify.ogg import OggParser  # noqa: E402


def credit_pipe_output(args):
    """Read each announced packet before acknowledging it, with no exit flush."""
    parent, child = socket.socketpair()
    parent.settimeout(5)
    try:
        with subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            pass_fds=(child.fileno(),),
            env={**os.environ, "KALINKA_CONTROL_FD": str(child.fileno())},
        ) as process:
            child.close()
            try:
                with parent.makefile("rb") as events:
                    ready = json.loads(events.readline())
                    assert ready["event"] == "ready" and ready["protocol"] == 1
                    output = bytearray()
                    packet_count = 0
                    for line in events:
                        event = json.loads(line)
                        assert event["event"] == "packet"
                        remaining = event["length"]
                        assert 0 < remaining <= 1024 * 1024
                        while remaining:
                            readable, _, _ = select.select([process.stdout], [], [], 5)
                            if not readable:
                                raise AssertionError(
                                    "Pipe retained announced bytes while waiting for credit"
                                )
                            data = os.read(process.stdout.fileno(), remaining)
                            assert data, (
                                "Pipe closed before its announced packet arrived"
                            )
                            output.extend(data)
                            remaining -= len(data)
                        parent.sendall(
                            json.dumps({"op": "ack", "id": event["id"]}).encode()
                            + b"\n"
                        )
                        packet_count += 1
                    assert packet_count > 1
                assert process.wait(timeout=5) == 0
                return bytes(output)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
    finally:
        parent.close()
        child.close()


binary = sys.argv[1]
cap = subprocess.check_output([binary, "--kalinka-capabilities"], timeout=5)
assert json.loads(cap) == {
    "protocol": 1,
    "librespot": "0.8.0",
    "passthrough": True,
    "pipe": True,
    "volume": True,
    "reconnect": True,
    "suspend": True,
}
for credit_gated in (False, True):
    for seek in (None, 4000):
        args = [binary, "--kalinka-self-test", str(ROOT / "tests/fixtures/tone.ogg")]
        if seek is not None:
            args.append(str(seek))
        output = (
            credit_pipe_output(args)
            if credit_gated
            else subprocess.check_output(args, timeout=10)
        )
        parser = OggParser()
        pages = parser.feed(output)
        parser.finish()
        assert pages[0].bos and pages[-1].eos
        assert sum(page.bos for page in pages) == 1
        assert (
            pages[-1].time_ms == 8000
            if seek is None
            else 3000 <= pages[-1].time_ms <= 4500
        )
        print(
            f"Validated binary pipe output: credit_gated={credit_gated}, seek={seek}, "
            f"bytes={len(output)}, duration_ms={pages[-1].time_ms}"
        )
