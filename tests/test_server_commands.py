"""Command-handling tests for ClientSession over a socketpair."""
import socket
import struct
import threading
from pathlib import Path

import pytest

from yail.config import ImageGenConfig, ServerConfig
from yail.protocol import ERROR_BLOCK, GRAPHICS_9
from yail.server import ClientSession, strip_quotes

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_IMAGE = str(REPO_ROOT / "test_images" / "yail_apple_splash1.jpg")


@pytest.fixture()
def gen_config(monkeypatch):
    monkeypatch.delenv("GEN_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    return ImageGenConfig()


def run_session(commands: bytes, gen_config, filenames=None, timeout=10.0,
                files_enabled=True, streaming_enabled=True) -> bytes:
    """Run a ClientSession against a socketpair; return everything it sent."""
    server_sock, client_sock = socket.socketpair()
    config = ServerConfig(files_enabled=files_enabled,
                          streaming_enabled=streaming_enabled)
    session = ClientSession(server_sock, 1, config, gen_config, filenames or [])

    def run():
        try:
            session.run()
        except Exception:
            pass
        finally:
            server_sock.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    client_sock.sendall(commands)
    client_sock.shutdown(socket.SHUT_WR)

    received = b""
    client_sock.settimeout(timeout)
    try:
        while True:
            chunk = client_sock.recv(65536)
            if not chunk:
                break
            received += chunk
    except socket.timeout:
        pass
    client_sock.close()
    thread.join(timeout=2)
    return received


def test_strip_quotes():
    assert strip_quotes('"two words"') == "two words"
    assert strip_quotes("plain") == "plain"
    assert strip_quotes('"') == '"'


def test_unknown_command_acks(gen_config):
    received = run_session(b"bogus-command quit", gen_config)
    assert received == b"OK: ACK!\r\n"


def test_http_request_rejected(gen_config):
    received = run_session(b"GET / HTTP/1.1\r\n\r\n", gen_config)
    assert received.startswith(b"HTTP/1.1 403 Forbidden")
    assert received.endswith(b"Not Allowed")


def test_gfx_then_files_streams_yai_packet(gen_config):
    received = run_session(b"gfx 4 files quit", gen_config, filenames=[TEST_IMAGE])
    # v1.1 header with the requested mode, then 8800 framebuffer bytes.
    assert received[:3] == bytes([1, 1, 0])
    assert received[3] == GRAPHICS_9
    assert received[4] == 0x03
    assert struct.unpack("<H", received[5:7])[0] == 8800
    assert len(received) == 7 + 8800


def test_files_with_no_files_sends_error_packet(gen_config):
    received = run_session(b"files quit", gen_config, filenames=[])
    assert received[:3] == bytes([1, 4, 0])
    assert received[5] == ERROR_BLOCK
    msg_len = struct.unpack("<I", received[6:10])[0]
    assert received[10:10 + msg_len] == b"No image files available"


def test_files_disabled_sends_error_packet(gen_config):
    # Even with files indexed, the 'files' command requires explicit opt-in.
    received = run_session(b"files quit", gen_config, filenames=[TEST_IMAGE],
                           files_enabled=False)
    assert received[:3] == bytes([1, 4, 0])
    assert received[5] == ERROR_BLOCK
    msg_len = struct.unpack("<I", received[6:10])[0]
    assert received[10:10 + msg_len] == b"File serving is disabled on this server"


def test_next_with_streaming_disabled_sends_error(gen_config):
    received = run_session(b"gfx 4 files next quit", gen_config,
                           filenames=[TEST_IMAGE], streaming_enabled=False)
    # The files image streams normally; the 'next' is refused.
    assert received[:3] == bytes([1, 1, 0])
    error_start = 7 + 8800
    packet = received[error_start:]
    assert packet[:3] == bytes([1, 4, 0])
    assert packet[5] == ERROR_BLOCK
    msg_len = struct.unpack("<I", packet[6:10])[0]
    assert packet[10:10 + msg_len] == b"Streaming is disabled on this server"


def test_next_without_mode_sends_error(gen_config):
    received = run_session(b"next quit", gen_config)
    assert received[5] == ERROR_BLOCK


def test_gen_routes_model_and_prompt(gen_config, monkeypatch):
    seen = {}

    def fake_generate(prompt, cfg, model=None):
        seen["prompt"] = prompt
        seen["model"] = model
        return TEST_IMAGE, None

    monkeypatch.setattr("yail.server.generate_image", fake_generate)
    received = run_session(b'gen gpt-image-1 "a red rocket" quit', gen_config)

    # The model the client requested is honored and quotes are stripped.
    assert seen["model"] == "gpt-image-1"
    assert seen["prompt"] == "a red rocket"
    assert received[:3] == bytes([1, 1, 0])  # image streamed


def test_gen_without_model_uses_server_default(gen_config, monkeypatch):
    seen = {}

    def fake_generate(prompt, cfg, model=None):
        seen["prompt"] = prompt
        seen["model"] = model
        return TEST_IMAGE, None

    monkeypatch.setattr("yail.server.generate_image", fake_generate)
    received = run_session(b'gen "a red rocket" quit', gen_config)

    # No model token: the server's configured model decides.
    assert seen["model"] is None
    assert seen["prompt"] == "a red rocket"
    assert received[:3] == bytes([1, 1, 0])


def test_gen_bare_word_treated_as_prompt(gen_config, monkeypatch):
    seen = {}

    def fake_generate(prompt, cfg, model=None):
        seen["prompt"] = prompt
        seen["model"] = model
        return TEST_IMAGE, None

    monkeypatch.setattr("yail.server.generate_image", fake_generate)
    # Unquoted prompts consume the rest of the buffer (legacy tokenizer),
    # so no trailing 'quit' here; the session ends with the socket.
    run_session(b"gen sailboat", gen_config)
    assert seen["model"] is None
    assert seen["prompt"] == "sailboat"


def test_search_uses_ddgs_results(gen_config, monkeypatch):
    monkeypatch.setattr("yail.server.search_images",
                        lambda term, max_images=1000, backends=None: [])
    received = run_session(b'search "anything" quit', gen_config)
    assert received[5] == ERROR_BLOCK  # no images found -> error packet


def test_openai_config_query(gen_config):
    received = run_session(b"openai-config quit", gen_config)
    assert received.startswith(b"OK: Current OpenAI config:")


def test_gfx_invalid_value_sends_error(gen_config):
    received = run_session(b"gfx banana quit", gen_config)
    assert received[5] == ERROR_BLOCK
