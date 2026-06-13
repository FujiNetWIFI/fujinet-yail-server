"""Local image file collection for the 'files' command."""
import logging
import os

logger = logging.getLogger(__name__)


def collect_files(paths: list[str], extensions: list[str]) -> list[str]:
    """Collect image file paths from directories and/or explicit file lists."""
    extensions = [ext.lower() if ext.startswith(".") else f".{ext.lower()}"
                  for ext in extensions]
    filenames: list[str] = []

    def consider(file_path: str) -> None:
        _, ext = os.path.splitext(file_path)
        if ext.lower() in extensions:
            filenames.append(file_path)

    for path in paths:
        if os.path.isdir(path):
            for root, _, files in os.walk(path):
                for file in files:
                    consider(os.path.join(root, file))
        else:
            consider(path)

    logger.info(f"Collected {len(filenames)} image files from {paths or 'no paths'}")
    return filenames
