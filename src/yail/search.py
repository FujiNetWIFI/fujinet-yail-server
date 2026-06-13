"""Image search via the DDGS metasearch package."""
import logging

logger = logging.getLogger(__name__)


def search_images(term: str, max_images: int = 1000,
                  backends: list[str] | None = None) -> list[str]:
    """Search for images and return a list of image URLs (empty on failure).

    backends is an ordered list of DDGS image engine names; None or ["auto"]
    lets DDGS pick across all engines.
    """
    backend = ",".join(backends) if backends else "auto"
    try:
        from ddgs import DDGS
        results = DDGS().images(query=term, max_results=max_images, backend=backend)
        urls = [result["image"] for result in results]
        logger.info(f"Found {len(urls)} images for search term: '{term}' "
                    f"(backend: {backend})")
        return urls
    except Exception as e:
        logger.error(f"Error searching for images '{term}' (backend: {backend}): {e}")
        return []
