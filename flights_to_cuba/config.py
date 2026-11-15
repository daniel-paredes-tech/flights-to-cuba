"""Configuration helpers."""

import os
from pathlib import Path


def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines from a .env file. Variables already set are kept."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
