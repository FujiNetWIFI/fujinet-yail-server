"""Tests for the admin web UI JSON API."""
import json
import urllib.request

import pytest

from yail import __version__
from yail.config import ImageGenConfig, ServerConfig
from yail.stats import STATS
from yail.webui import WebUIContext, mask_key, start_webui


@pytest.fixture
def webui(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-1234567890abcd")
    monkeypatch.setenv("GEN_MODEL", "google/gemini-2.5-flash-image")
    gen_config = ImageGenConfig()
    server_config = ServerConfig()
    env_path = tmp_path / ".env"
    context = WebUIContext(gen_config, server_config, ["a.jpg", "b.png"],
                           env_path=str(env_path))
    httpd = start_webui("127.0.0.1", 0, context)
    port = httpd.server_address[1]
    yield f"http://127.0.0.1:{port}", gen_config, env_path, context
    httpd.shutdown()


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read())


def post_json(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def test_mask_key():
    assert mask_key(None) == ""
    assert mask_key("short") == "*****"
    assert mask_key("sk-test-1234567890abcd") == "sk-...abcd"


def test_dashboard_served(webui):
    base, _, _, _ = webui
    with urllib.request.urlopen(base + "/", timeout=5) as resp:
        body = resp.read().decode()
    assert resp.status == 200
    assert "YAIL SERVER" in body


def test_status(webui):
    base, _, _, _ = webui
    status = get_json(base + "/api/status")
    assert status["version"] == __version__
    assert status["local_files"] == 2
    assert status["openrouter_key_set"] is True
    assert status["gen_model"] == "google/gemini-2.5-flash-image"


def test_config_get_masks_keys(webui):
    base, _, _, _ = webui
    config = get_json(base + "/api/config")
    assert config["model"] == "google/gemini-2.5-flash-image"
    assert config["openrouter_api_key"] == "sk-...abcd"
    assert "sk-or-test" not in json.dumps(config)


def test_config_post_applies_and_persists(webui):
    base, gen_config, env_path, _ = webui
    result = post_json(base + "/api/config", {
        "model": "black-forest-labs/flux.2-pro",
        "openrouter_api_key": "sk-or-new-key-9876543210", "persist": True,
    })
    assert result["errors"] == []
    assert set(result["applied"]) == {"model", "openrouter_api_key"}
    assert result["persisted"] is True
    assert gen_config.model == "black-forest-labs/flux.2-pro"
    assert gen_config.api_key == "sk-or-new-key-9876543210"
    env_text = env_path.read_text()
    assert "GEN_MODEL" in env_text and "black-forest-labs/flux.2-pro" in env_text
    assert "OPENROUTER_API_KEY" in env_text and "sk-or-new-key-9876543210" in env_text


def test_config_post_legacy_model_resolved(webui):
    base, gen_config, _, _ = webui
    result = post_json(base + "/api/config", {"model": "gpt-image-1"})
    assert result["errors"] == []
    assert gen_config.model == "openai/gpt-image-1"


def test_stats_reflects_sessions_and_images(webui):
    base, _, _, _ = webui
    STATS.session_started(9001, "10.0.0.5:4242")
    STATS.session_update(9001, mode="search", last_command='search "cats"')
    STATS.image_event(9001, "search", "http://example.com/cat.jpg", 2, True, 0.25)
    try:
        stats = get_json(base + "/api/stats")
        session = next(s for s in stats["sessions"] if s["id"] == 9001)
        assert session["address"] == "10.0.0.5:4242"
        assert session["mode"] == "search"
        assert session["images_sent"] == 1
        assert any(i["target"] == "http://example.com/cat.jpg"
                   for i in stats["recent_images"])
        assert stats["counters"]["images_served"] >= 1
    finally:
        STATS.session_ended(9001)


def test_logs_endpoint(webui):
    base, _, _, _ = webui
    import logging
    from yail.stats import install_log_buffer
    install_log_buffer()
    test_logger = logging.getLogger("yail.test")
    test_logger.setLevel(logging.INFO)
    test_logger.info("hello from the test suite")
    logs = get_json(base + "/api/logs?n=50")["logs"]
    assert any("hello from the test suite" in entry["message"] for entry in logs)


def test_unknown_route_404(webui):
    base, _, _, _ = webui
    try:
        urllib.request.urlopen(base + "/api/nope", timeout=5)
        assert False, "expected 404"
    except urllib.error.HTTPError as e:
        assert e.code == 404


def test_files_config_set_path_and_enable(webui, tmp_path):
    base, _, env_path, context = webui
    folder = tmp_path / "imgs"
    folder.mkdir()
    (folder / "one.jpg").write_bytes(b"x")
    (folder / "two.png").write_bytes(b"x")
    (folder / "skip.txt").write_bytes(b"x")

    result = post_json(base + "/api/config", {
        "files_path": str(folder), "files_enabled": True, "persist": True,
    })
    assert result["errors"] == []
    assert "files_path" in result["applied"]
    assert "files_enabled" in result["applied"]
    assert context.server_config.files_enabled is True
    assert sorted(context.filenames) == [str(folder / "one.jpg"),
                                         str(folder / "two.png")]
    env_text = env_path.read_text()
    assert "FILES_ENABLED" in env_text and "true" in env_text
    assert str(folder) in env_text


def test_files_config_rejects_bad_path(webui):
    base, _, _, context = webui
    result = post_json(base + "/api/config",
                       {"files_path": "relative/or/missing"})
    assert any("files_path" in e for e in result["errors"])
    assert context.filenames == ["a.jpg", "b.png"]


def test_files_enable_requires_path(webui):
    base, _, _, context = webui
    result = post_json(base + "/api/config", {"files_enabled": True})
    assert any("folder path" in e for e in result["errors"])
    assert context.server_config.files_enabled is False


def test_streaming_config_applies_and_persists(webui):
    base, _, env_path, context = webui
    result = post_json(base + "/api/config", {
        "streaming_enabled": False, "stream_max_retries": 5,
        "stream_retry_wait": 0.5, "download_timeout": 10, "persist": True,
    })
    assert result["errors"] == []
    assert context.server_config.streaming_enabled is False
    assert context.server_config.stream_max_retries == 5
    assert context.server_config.stream_retry_wait == 0.5
    assert context.server_config.download_timeout == 10
    env_text = env_path.read_text()
    assert "STREAM_ENABLED" in env_text and "false" in env_text
    assert "STREAM_MAX_RETRIES" in env_text


def test_streaming_config_rejects_out_of_range(webui):
    base, _, _, context = webui
    result = post_json(base + "/api/config", {"stream_max_retries": 0})
    assert any("stream_max_retries" in e for e in result["errors"])
    assert context.server_config.stream_max_retries == 10


def test_search_backends_set_and_persist(webui):
    base, _, env_path, context = webui
    result = post_json(base + "/api/config",
                       {"search_backends": ["Bing", "duckduckgo"],
                        "search_max_results": 200, "persist": True})
    assert result["errors"] == []
    assert context.server_config.search_backends == ["bing", "duckduckgo"]
    assert context.server_config.search_max_results == 200
    env_text = env_path.read_text()
    assert "SEARCH_BACKENDS" in env_text and "bing,duckduckgo" in env_text
    assert "SEARCH_MAX_RESULTS" in env_text


def test_search_backends_reject_unknown_and_empty(webui):
    base, _, _, context = webui
    result = post_json(base + "/api/config", {"search_backends": ["altavista"]})
    assert any("altavista" in e for e in result["errors"])
    assert context.server_config.search_backends == ["auto"]

    result = post_json(base + "/api/config", {"search_backends": []})
    assert any("non-empty" in e for e in result["errors"])


def test_search_backends_auto_collapses(webui):
    base, _, _, context = webui
    result = post_json(base + "/api/config", {"search_backends": ["bing", "auto"]})
    assert result["errors"] == []
    assert context.server_config.search_backends == ["auto"]


def test_files_clearing_path_disables(webui, tmp_path):
    base, _, _, context = webui
    folder = tmp_path / "imgs2"
    folder.mkdir()
    (folder / "pic.jpg").write_bytes(b"x")
    post_json(base + "/api/config",
              {"files_path": str(folder), "files_enabled": True})
    assert context.server_config.files_enabled is True

    result = post_json(base + "/api/config", {"files_path": ""})
    assert result["errors"] == []
    assert context.filenames == []
    assert context.server_config.files_enabled is False
