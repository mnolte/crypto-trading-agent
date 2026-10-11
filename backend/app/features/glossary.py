"""The plain-language glossary: terms and their one-to-two-sentence
definitions, loaded once from ``app/glossary.yaml``. Terms render with a
dotted underline and a tooltip when plain-language mode is on; Ask answers
"what does X mean?" from the same file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from app.config import BACKEND_DIR

GLOSSARY_PATH = BACKEND_DIR / "app" / "glossary.yaml"


@lru_cache
def load_glossary(path: Path = GLOSSARY_PATH) -> dict[str, str]:
    """term (lowercase) -> definition. Missing file = empty glossary, never
    a crash — tooltips just stop rendering."""
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return {
        str(item["term"]).strip().lower(): str(item["definition"]).strip()
        for item in data
        if isinstance(item, dict) and item.get("term") and item.get("definition")
    }
