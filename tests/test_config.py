"""ImageGenConfig validation and environment handling tests."""
import pytest

from yail.config import ImageGenConfig


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("GEN_MODEL", "OPENAI_MODEL", "OPENAI_SIZE", "OPENAI_QUALITY",
                "OPENAI_STYLE", "OPENAI_API_KEY", "GEMINI_API_KEY",
                "OPENROUTER_API_KEY", "OPENAI_SYSTEM_PROMPT"):
        monkeypatch.delenv(var, raising=False)


def test_defaults():
    cfg = ImageGenConfig()
    assert cfg.model == ImageGenConfig.DEFAULT_MODEL
    assert cfg.api_key is None


def test_env_key_and_model(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("GEN_MODEL", "black-forest-labs/flux.2-pro")
    cfg = ImageGenConfig()
    assert cfg.api_key == "sk-or-test"
    assert cfg.model == "black-forest-labs/flux.2-pro"


def test_legacy_model_names_resolve_to_openrouter_ids(monkeypatch):
    cfg = ImageGenConfig()
    # Bare legacy names from deployed clients map to OpenRouter ids.
    assert cfg.resolve_model("gpt-image-1") == "openai/gpt-image-1"
    assert cfg.resolve_model("gemini") == ImageGenConfig.DEFAULT_GEMINI_MODEL
    assert cfg.resolve_model("gemini-2.5-flash-image") == ImageGenConfig.DEFAULT_GEMINI_MODEL
    # Retired dall-e models are served with the configured default.
    assert cfg.resolve_model("dall-e-3") == cfg.DEFAULT_MODEL
    # Full OpenRouter ids pass through untouched.
    assert cfg.resolve_model("recraft/v4-pro") == "recraft/v4-pro"
    assert cfg.resolve_model(None) == cfg.DEFAULT_MODEL


def test_invalid_model_falls_back(monkeypatch):
    monkeypatch.setenv("GEN_MODEL", "stable-diffusion-9000")
    assert ImageGenConfig().model == ImageGenConfig.DEFAULT_MODEL


def test_is_valid_model_distinguishes_models_from_prompts():
    cfg = ImageGenConfig()
    assert cfg.is_valid_model("google/gemini-2.5-flash-image")
    assert cfg.is_valid_model("gpt-image-1")
    assert cfg.is_valid_model("dall-e-3")
    assert cfg.is_valid_model("gemini")
    assert not cfg.is_valid_model("sailboat")
    assert not cfg.is_valid_model("")


def test_set_model_resolves_and_rejects_empty():
    cfg = ImageGenConfig()
    assert cfg.set_model("openai/gpt-image-1")
    assert cfg.model == "openai/gpt-image-1"
    assert cfg.set_model("gemini")
    assert cfg.model == ImageGenConfig.DEFAULT_GEMINI_MODEL
    assert not cfg.set_model("")
    assert not cfg.set_model("   ")


def test_legacy_setters_accept_anything():
    # Kept for wire-protocol compatibility ('openai-config' command);
    # generation via OpenRouter chat completions ignores these.
    cfg = ImageGenConfig()
    assert cfg.set_size("1792x1024")
    assert cfg.set_quality("hd")
    assert cfg.set_style("natural")


def test_set_system_prompt_returns_true():
    # The legacy implementation returned None, which made the server report
    # an error to the client even when the prompt was set successfully.
    cfg = ImageGenConfig()
    assert cfg.set_system_prompt("draw like it's 1979") is True
    assert cfg.system_prompt == "draw like it's 1979"
