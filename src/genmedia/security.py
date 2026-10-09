"""Small redaction helpers, not a sandbox or an exhaustive secret detector."""
from __future__ import annotations

import os
import re
from collections.abc import Mapping


def sensitive_env_name(name: str) -> bool:
    name = name.upper()
    return (
        name in {"REDIS_URL", "SUNO_CALLBACK_URL", "GOOGLE_APPLICATION_CREDENTIALS"}
        or any(part in name for part in ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"))
        or name.endswith("ACCESS_KEY_ID")
    )


def redact(text: object, *, limit: int = 500, environ: Mapping[str, str] | None = None) -> str:
    """Remove known credentials, URL capabilities and common auth assignments.

    Do not use this as permission to log complete provider requests/responses.
    Unknown credential formats can still require a manual review.
    """
    value = str(text)
    env = os.environ if environ is None else environ
    secrets = sorted((v for k, v in env.items() if sensitive_env_name(k) and len(v) >= 4),
                     key=len, reverse=True)
    for secret in secrets:
        value = value.replace(secret, "<redacted>")
    value = re.sub(r"(?i)\b(?:https?|rediss?)://[^\s<>\"']+", "<redacted-url>", value)
    value = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+", "Bearer <redacted>", value)
    value = re.sub(
        r"(?i)([?&](?:key|api_key|apikey|token|signature|sig)=)[^&\s]+",
        r"\1<redacted>", value,
    )
    value = re.sub(
        r"(?i)((?:x-goog-api-key|api[_-]?key|access[_-]?token|secret|password)"
        r"[\"']?\s*[:=]\s*[\"']?)[^\s,;}\"']+",
        r"\1<redacted>", value,
    )
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value).strip()
    return value if len(value) <= limit else value[:limit] + "..."


def render_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """Remove standard credential variables; children are NOT isolated from disk."""
    return {key: value for key, value in environ.items() if not sensitive_env_name(key)}
