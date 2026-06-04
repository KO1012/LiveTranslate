from __future__ import annotations

import logging
import re
from typing import Any


SECRET_KEYS = {"api_key", "token", "authorization", "password", "secret"}

SECRET_PATTERNS = (
    re.compile(
        r"(?i)\b(api[_ -]?key|authorization|bearer|token|x-livetranslate-token)"
        r"(\s*[:=]\s*)(['\"]?)([^'\"\s,;]+)(['\"]?)"
    ),
    re.compile(r"(?i)\b(sk-[A-Za-z0-9_\-]{8,})\b"),
)

# Credentials embedded in a URL userinfo (e.g. a custom proxy URL like
# http://user:pass@host:port). Handled separately because its replacement
# rebuilds the URL prefix rather than masking a single secret token. Masking
# the "user:pass@" portion keeps proxy credentials out of logs/diagnostics.
_URL_USERINFO_PATTERN = re.compile(
    r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/@\s:]+):([^/@\s]+)@"
)


def _replace_url_userinfo(match: re.Match) -> str:
    scheme, user, password = match.groups()
    return f"{scheme}{mask_secret(user)}:{mask_secret(password)}@"


def redact_secret_text(value: Any) -> str:
    text = str(value)
    text = _URL_USERINFO_PATTERN.sub(_replace_url_userinfo, text)
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(_replace_secret, text)
    return text


def _replace_secret(match: re.Match) -> str:
    if len(match.groups()) == 1:
        return mask_secret(match.group(1))
    name, sep, quote, secret, end_quote = match.groups()
    return f"{name}{sep}{quote}{mask_secret(secret)}{end_quote}"


def mask_secret(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}****{value[-4:]}"


def redact_secret_data(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if str(key).lower() in SECRET_KEYS:
                redacted[key] = "****" if item else ""
            else:
                redacted[key] = redact_secret_data(item)
        return redacted
    if isinstance(value, list):
        return [redact_secret_data(item) for item in value]
    if isinstance(value, str):
        return redact_secret_text(value)
    return value


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_secret_text(record.getMessage())
        record.args = ()
        return True


def add_secret_redaction(handler: logging.Handler) -> logging.Handler:
    handler.addFilter(SecretRedactionFilter())
    return handler
