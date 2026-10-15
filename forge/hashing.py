from __future__ import annotations

import hashlib
import json
from typing import Any


SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "access_token",
    "headers",
    "secret",
    "token",
}


def strip_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): strip_secrets(item)
            for key, item in value.items()
            if str(key).lower() not in SECRET_KEYS
        }
    if isinstance(value, list):
        return [strip_secrets(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(strip_secrets(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def arguments_hash(arguments: dict) -> str:
    return sha256_text(canonical_json(arguments))
