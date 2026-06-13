"""Image generation via OpenRouter.

OpenRouter exposes an OpenAI-compatible chat completions API; image models
are invoked with modalities=["image", "text"] and return the image as a
base64 data URL in the assistant message's `images` field.
"""
import base64
import logging
import os
import time
import traceback

from yail.config import ImageGenConfig
from yail.gen.util import short_error

logger = logging.getLogger(__name__)

try:
    import openai
    OPENROUTER_AVAILABLE = True
except ImportError:
    OPENROUTER_AVAILABLE = False
    logger.warning("openai library not available. Install with: pip install openai")

GENERATED_DIR = "generated_images"


def _save_b64_image(b64_data: str, prefix: str) -> str:
    os.makedirs(GENERATED_DIR, exist_ok=True)
    path = os.path.join(GENERATED_DIR, f"{prefix}-{int(time.time())}.png")
    with open(path, "wb") as f:
        f.write(base64.b64decode(b64_data))
    return os.path.abspath(path)


def _image_url(entry) -> str | None:
    """Extract the URL from one entry of the message's images list."""
    if isinstance(entry, dict):
        image_url = entry.get("image_url") or {}
        if isinstance(image_url, dict):
            return image_url.get("url")
        return getattr(image_url, "url", None)
    image_url = getattr(entry, "image_url", None)
    if isinstance(image_url, dict):
        return image_url.get("url")
    return getattr(image_url, "url", None)


def generate_image_with_openrouter(prompt: str, gen_config: ImageGenConfig,
                                   model: str | None = None
                                   ) -> tuple[str | None, str | None]:
    """Generate an image; return (url_or_path, None) or (None, reason)."""
    if not OPENROUTER_AVAILABLE:
        return None, "openai library not installed on server"
    if not gen_config.api_key:
        logger.error("OpenRouter API key not provided. Set OPENROUTER_API_KEY.")
        return None, "OpenRouter API key not configured on server"

    model = gen_config.resolve_model(model or gen_config.model)

    try:
        logger.info(f"Generating image via OpenRouter model: {model}, "
                    f"prompt: '{prompt}'")
        client = openai.OpenAI(base_url=gen_config.OPENROUTER_BASE_URL,
                               api_key=gen_config.api_key)
        messages = []
        if gen_config.system_prompt:
            messages.append({"role": "system", "content": gen_config.system_prompt})
        messages.append({"role": "user", "content": prompt})

        response = client.chat.completions.create(
            model=model,
            messages=messages,
            extra_body={"modalities": ["image", "text"]},
        )

        message = response.choices[0].message
        images = getattr(message, "images", None)
        if not images and getattr(message, "model_extra", None):
            images = message.model_extra.get("images")
        if not images:
            text = (message.content or "").strip()
            logger.error(f"OpenRouter returned no image. Text: {text[:200]}")
            reason = "Model returned no image"
            if text:
                reason += f": {text[:90]}"
            return None, reason

        url = _image_url(images[0])
        if not url:
            return None, "Unrecognized image payload from OpenRouter"
        if url.startswith("data:"):
            b64_data = url.split(",", 1)[1]
            path = _save_b64_image(b64_data, "openrouter")
            logger.info(f"Image generated successfully via OpenRouter: {path}")
            return path, None
        if url.startswith("http"):
            logger.info(f"Image generated successfully via OpenRouter: {url}")
            return url, None
        return None, "Unrecognized image URL format from OpenRouter"

    except Exception as e:
        logger.error(f"Error generating image via OpenRouter: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return None, short_error(e)
