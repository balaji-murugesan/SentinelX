"""Masks secrets and account identifiers before anything is logged or exported as telemetry.

Pure functions, no I/O. Used by the log pipeline (observability.logging_setup) and the span
exporter (observability.tracing). CLAUDE.md safety rule 10: no secrets in code, logs, tests or
traces; tokens and account IDs are masked.

Three layers, because no single one is enough:
  1. By key name   - a field called ``access_token`` is masked whatever its value looks like.
  2. By type       - pydantic ``SecretStr`` values are masked.
  3. By content    - free text and non-secret keys are scrubbed for URLs with passwords,
                     ``Bearer ...`` headers, JWTs (Dhan access tokens are JWTs) and
                     ``password=...`` style pairs.

Limit: a secret pasted into free text in an unrecognisable shape cannot be detected. Do not
log request/response bodies from the broker.
"""

import re
from typing import Any

from pydantic import SecretBytes, SecretStr

MASK = "***"

# Field names whose values are always masked. "key" alone is NOT here: idempotency_key must
# stay visible for tracing orders, so only credential-like key names are listed.
_SECRET_KEY = re.compile(
    r"token|secret|passw(or)?d|authorization|api[_-]?key|access[_-]?key|credential|cookie"
    r"|private[_-]?key|dsn|connection[_-]?string",
    re.IGNORECASE,
)

# account_id, client_id, client_code and prefixed forms such as dhan_client_id.
_ACCOUNT_KEY = re.compile(r"(^|_)(account|client)_(id|code)$", re.IGNORECASE)

_URL_PASSWORD = re.compile(r"(?P<head>[a-z][a-z0-9+.\-]*://[^:/@\s]*:)[^@\s/]+(?=@)", re.IGNORECASE)
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=\-]+", re.IGNORECASE)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*")
_SECRET_PAIR = re.compile(
    r"(?P<name>\b(?:access[_-]?token|token|secret|passw(?:or)?d|api[_-]?key|authorization)\b)"
    r"(?P<sep>\s*[=:]\s*)(?P<value>\"[^\"]*\"|'[^']*'|(?:Bearer\s+)?[^\s,;&]+)",
    re.IGNORECASE,
)

_MAX_DEPTH = 8


def is_secret_key(key: object) -> bool:
    return bool(_SECRET_KEY.search(str(key)))


def is_account_key(key: object) -> bool:
    return bool(_ACCOUNT_KEY.search(str(key)))


def mask_account(value: object) -> str:
    """Keep only the last four characters: ``1234567890`` -> ``****7890``."""
    text = str(value)
    return "****" + text[-4:] if len(text) > 4 else "****"


def scrub_text(text: str) -> str:
    """Remove secrets that are recognisable by their shape inside free text."""
    text = _URL_PASSWORD.sub(lambda m: m.group("head") + MASK, text)
    text = _BEARER.sub("Bearer " + MASK, text)
    text = _JWT.sub(MASK, text)
    return _SECRET_PAIR.sub(lambda m: m.group("name") + m.group("sep") + MASK, text)


def mask_value(value: Any, key: object = None, _depth: int = 0) -> Any:
    """Return ``value`` with secrets masked. ``key`` is the field name it was stored under."""
    if isinstance(value, SecretStr | SecretBytes):
        return MASK
    if key is not None:
        if is_secret_key(key) and value not in (None, ""):
            return MASK
        if is_account_key(key) and value not in (None, ""):
            return mask_account(value)
    if _depth >= _MAX_DEPTH:
        return "[too deep to mask]" if isinstance(value, dict | list | tuple | set) else value
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {k: mask_value(v, k, _depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return type(value)(mask_value(v, key, _depth + 1) for v in value)
    return value
