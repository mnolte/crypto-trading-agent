"""Versioned prompt files.

A prompt file starts with an HTML-comment header carrying
``prompt_version:`` and a ``model_parameters:`` block (``temperature``,
``max_output_tokens``); everything after ``-->`` is the system prompt body
sent as ``instructions``. Files are immutable once in use — a change is a
new ``decision.vN.md``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.config import BACKEND_DIR

PROMPTS_DIR = BACKEND_DIR / "prompts"


@dataclass
class LoadedPrompt:
    version: str
    body: str
    temperature: float
    max_output_tokens: int
    brief_version: int = 1


class PromptNotFoundError(Exception):
    pass


def list_prompt_versions() -> list[str]:
    return sorted(
        p.stem for p in PROMPTS_DIR.glob("decision.v*.md") if _version(p.read_text()[:1200])
    )


def _version(head: str) -> str | None:
    match = re.search(r"prompt_version:\s*(\S+)", head)
    return match.group(1) if match else None


def load_prompt(version: str, *, prompts_dir: Path = PROMPTS_DIR) -> LoadedPrompt:
    """Load ``decision.vN.md`` — its body plus the model parameters the
    header pins."""
    # The settings select offers "decision.v1"; the file shares the name.
    name = version if version.endswith(".md") else f"{version}.md"
    path = prompts_dir / name
    if not path.is_file():
        raise PromptNotFoundError(f"prompt {version} not found")
    text = path.read_text()
    head = text[:1200]
    if _version(head) != version:
        raise PromptNotFoundError(f"prompt {version} not found in {name}")
    temp = re.search(r"temperature:\s*(\S+)", head)
    tokens = re.search(r"max_output_tokens:\s*(\d+)", head)
    brief_v = re.search(r"brief_version:\s*(\d+)", head)
    # The system prompt is the body after the header comment.
    body = text.split("-->", 1)[1].strip() if "-->" in text else text.strip()
    return LoadedPrompt(
        version=version,
        body=body,
        temperature=float(temp.group(1)) if temp else 0.0,
        max_output_tokens=int(tokens.group(1)) if tokens else 600,
        brief_version=int(brief_v.group(1)) if brief_v else 1,
    )
