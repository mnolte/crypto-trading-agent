"""Secret scrubbing, applied before a line is queued.

Replaces, with ``[redacted]``:

- the exact value of every registered secret (stored keys, the SMTP
  password — registered at startup and whenever one is set);
- anything that looks like a key or token (``sk-…`` OpenAI-style keys,
  bearer tokens, JWTs, long hex/base64 secrets after ``key``/``token``/
  ``secret``/``password`` markers);
- Authorization and Cookie header values.

Applied to the message, the ``data`` JSON and exception messages, so a
secret inside a traceback is scrubbed too. Scrubbing is idempotent and
never raises — a broken scrubber must not break logging.
"""

from __future__ import annotations

import re
import threading

REDACTED = "[redacted]"

_lock = threading.Lock()
_secrets: set[str] = set()

# Key-looking material. Patterns are deliberately anchored to a marker
# word or a known prefix so ordinary prose and prices survive.
_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=\-]{8,}", re.IGNORECASE),
    re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}"),
    re.compile(
        r"((?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|token|secret|password|passwd)"
        r"[\s\"']*[:=][\s\"']*)([^\s\"',;}]{4,})",
        re.IGNORECASE,
    ),
    re.compile(r"(Authorization[\"']?\s*:\s*[\"']?)(\S+(?:\s+\S+)?[\"']?)", re.IGNORECASE),
    re.compile(r"(Cookie[\"']?\s*:\s*[\"']?)([^\r\n\",}]*(?:[\",}])?)", re.IGNORECASE),
    re.compile(r"(X-Local-Token[\"']?\s*:\s*[\"']?)(\S+)", re.IGNORECASE),
]


def register_secret(value: str | None) -> None:
    """Add an exact secret value to redact wherever it appears."""
    if value and len(value) >= 4:
        with _lock:
            _secrets.add(value)


def forget_secrets() -> None:
    """Tests reset registered secrets between cases."""
    with _lock:
        _secrets.clear()


def scrub_text(text: str) -> str:
    """Return ``text`` with every secret and key-like value redacted."""
    if not text:
        return text
    out = text
    with _lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for secret in secrets:
        if secret in out:
            out = out.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        if pattern.groups == 2:
            out = pattern.sub(lambda m: m.group(1) + REDACTED, out)
        else:
            out = pattern.sub(REDACTED, out)
    return out
