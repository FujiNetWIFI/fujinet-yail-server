"""Image generation dispatch.

All generation goes through OpenRouter (one API key, any model). Returns
(result, error): result is an http(s) URL or a local file path to the
image; on failure result is None and error is a short human-readable
reason suitable for the client error packet.
"""
import logging

from yail.config import ImageGenConfig
from yail.gen.openrouter_backend import (OPENROUTER_AVAILABLE,
                                         generate_image_with_openrouter)
from yail.gen.util import short_error  # noqa: F401  (re-export)

logger = logging.getLogger(__name__)


def generate_image(prompt: str, gen_config: ImageGenConfig,
                   model: str | None = None) -> tuple[str | None, str | None]:
    """Generate an image; return (url_or_path, None) or (None, reason)."""
    return generate_image_with_openrouter(prompt, gen_config, model=model)
