"""Configuration for the YAIL server and its image-generation backend.

Settings precedence (lowest to highest): process environment, env file
(server/env or .env via python-dotenv), command-line arguments.
"""
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

DEFAULT_PORT = 5556
DEFAULT_EXTENSIONS = [".jpg", ".jpeg", ".gif", ".png"]


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    paths: list[str] = field(default_factory=list)
    extensions: list[str] = field(default_factory=lambda: list(DEFAULT_EXTENSIONS))
    camera: str | None = None
    enable_camera: bool = False
    # Local file serving is opt-in: the 'files' command only works when this
    # is true AND an explicit folder path was configured (CLI, env, or UI).
    files_enabled: bool = False
    # Streaming: the client slideshow loop driven by the 'next' command.
    streaming_enabled: bool = True
    stream_max_retries: int = 10     # attempts before giving up on a source list
    stream_retry_wait: float = 1.0   # seconds between retries on a bad image
    download_timeout: float = 5.0    # seconds to fetch a remote image
    # Image search: ordered DDGS engine names, or ["auto"] for all engines.
    search_backends: list[str] = field(default_factory=lambda: ["auto"])
    search_max_results: int = 1000


def valid_search_backends() -> list[str]:
    """Image search engines available in the installed ddgs package."""
    try:
        from ddgs.engines import ENGINES
        engines = sorted(set(ENGINES.get("images", [])))
    except Exception:
        engines = ["bing", "duckduckgo"]
    return ["auto"] + engines


class ImageGenConfig:
    """Settings for image generation via OpenRouter.

    A single OPENROUTER_API_KEY covers every model; OpenRouter model names
    use the vendor/model form (e.g. google/gemini-2.5-flash-image). Bare
    legacy names from deployed clients (gpt-image-1, dall-e-3, gemini) are
    resolved to OpenRouter equivalents.
    """

    OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
    DEFAULT_MODEL = "google/gemini-2.5-flash-image"
    # The 'gen-gemini' wire command pins this model.
    DEFAULT_GEMINI_MODEL = "google/gemini-2.5-flash-image"

    # Bare model-name prefixes that deployed clients may still send.
    LEGACY_MODEL_PREFIXES = ("gpt-image", "dall-e", "gemini")

    def __init__(self) -> None:
        self.api_key = os.environ.get("OPENROUTER_API_KEY")
        self.model = self.resolve_model(
            os.environ.get("GEN_MODEL", self.DEFAULT_MODEL))
        self.system_prompt = os.environ.get(
            "OPENAI_SYSTEM_PROMPT",
            "You are an image generation assistant. Generate an image based "
            "on the user's description.",
        )

        # Vestigial settings kept so the legacy 'openai-config' wire command
        # keeps answering OK for clients in the wild; generation ignores them.
        self.size = os.environ.get("OPENAI_SIZE", "1024x1024")
        self.quality = os.environ.get("OPENAI_QUALITY", "auto")
        self.style = os.environ.get("OPENAI_STYLE", "vivid")

        logger.info(f"ImageGenConfig initialized with model: {self.model}")
        logger.info(f"OPENROUTER_API_KEY: {'set' if self.api_key else 'not set'}")

    def is_valid_model(self, model: str) -> bool:
        """True if the string plausibly names a model (vs. a prompt word)."""
        if not model:
            return False
        if "/" in model:
            return True
        return any(model.lower().startswith(p) for p in self.LEGACY_MODEL_PREFIXES)

    def resolve_model(self, model: str | None) -> str:
        """Map a requested model to an OpenRouter model id."""
        if not model:
            return self.DEFAULT_MODEL
        if "/" in model:
            return model
        bare = model.lower()
        if bare.startswith("gpt-image"):
            return f"openai/{bare}"
        if "gemini" in bare:
            return self.DEFAULT_GEMINI_MODEL
        if bare.startswith("dall-e"):
            # Retired models; serve with the configured default instead.
            logger.warning(f"Requested retired model '{model}'; "
                           f"using {self.DEFAULT_MODEL}")
            return self.DEFAULT_MODEL
        logger.warning(f"Unknown model format: {model}. "
                       f"Using default: {self.DEFAULT_MODEL}")
        return self.DEFAULT_MODEL

    def set_model(self, model: str) -> bool:
        if not model or not model.strip():
            return False
        self.model = self.resolve_model(model.strip())
        logger.info(f"Model set to: {self.model}")
        return True

    # Accepted-and-stored for wire-protocol compatibility; unused by the
    # OpenRouter chat-completions image flow.
    def set_size(self, size: str) -> bool:
        self.size = size
        return True

    def set_quality(self, quality: str) -> bool:
        self.quality = quality
        return True

    def set_style(self, style: str) -> bool:
        self.style = style
        return True

    def set_api_key(self, api_key: str) -> None:
        self.api_key = api_key
        logger.info("OpenRouter API key updated")

    def set_system_prompt(self, system_prompt: str) -> bool:
        self.system_prompt = system_prompt
        logger.info(f"System prompt updated: {system_prompt}")
        return True

    def __str__(self) -> str:
        return f"ImageGenConfig(model={self.model})"
