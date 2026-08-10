from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


ENV_KEYS = {
    "serper": "SERPER_API_KEY",
    "brave": "BRAVE_API_KEY",
    "tavily": "TAVILY_API_KEY",
    "exa": "EXA_API_KEY",
    "newsapi": "NEWS_API_KEY",
    "github": "GITHUB_TOKEN",
    "firecrawl": "FIRECRAWL_API_KEY",
    "baidu": "BAIDU_API_KEY",
}


DEFAULT_PROVIDER_WEIGHTS = {
    "serper": 1.00,
    "brave": 1.00,
    "tavily": 0.95,
    "exa": 1.00,
    "newsapi": 1.00,
    "github": 1.10,
    "firecrawl": 0.90,
    "baidu": 1.00,
}


def resolve_profile_path(profile_path: str | None = None) -> str | None:
    """Return an explicit profile path, or the process-level default path."""
    return profile_path if profile_path is not None else os.getenv("SEARCHX_PROFILE")


def finite_float(value: object) -> float | None:
    """Convert a profile number only when it is a real, finite value."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _positive_finite_env(name: str, default: float) -> float:
    value = finite_float(os.getenv(name, ""))
    return value if value is not None and value > 0 else default


def _nonnegative_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _positive_int_env(name: str, default: int) -> int:
    value = _nonnegative_int_env(name, default)
    return value if value > 0 else default


@dataclass(slots=True)
class Settings:
    timeout: float = 12.0
    retries: int = 1
    max_workers: int = 4
    result_limit: int = 10
    rrf_k: int = 60
    domain_cap: int = 2
    provider_weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_PROVIDER_WEIGHTS))

    @classmethod
    def load(cls, profile_path: str | None = None) -> "Settings":
        # Credentials are intentionally loaded before an engine constructs its
        # providers.  Explicit process environment values still take precedence
        # over the local file (see ``secrets.load_secrets``).
        load_local_secrets()
        settings = cls(
            timeout=_positive_finite_env("SEARCHX_TIMEOUT", 12.0),
            retries=_nonnegative_int_env("SEARCHX_RETRIES", 1),
            max_workers=_positive_int_env("SEARCHX_MAX_WORKERS", 4),
            result_limit=_positive_int_env("SEARCHX_RESULT_LIMIT", 10),
        )
        path = resolve_profile_path(profile_path)
        if path:
            try:
                obj = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                return settings
            if not isinstance(obj, dict):
                return settings
            weights = obj.get("provider_weights")
            if not isinstance(weights, dict):
                return settings
            for name, value in weights.items():
                if name not in settings.provider_weights:
                    continue
                number = finite_float(value)
                if number is not None:
                    settings.provider_weights[name] = number
        return settings


def load_local_secrets() -> set[str]:
    """Load local provider credentials without making configuration mandatory.

    The import is deliberately local: ``secrets`` uses ``ENV_KEYS`` from this
    module, and configuration should remain usable when the secrets file is
    absent or malformed.
    """
    from .secrets import load_secrets

    return load_secrets()


def api_key(provider: str) -> str | None:
    load_local_secrets()
    env = ENV_KEYS.get(provider)
    return os.getenv(env, "").strip() or None if env else None


def credential_status() -> dict[str, dict[str, Any]]:
    load_local_secrets()
    return {
        provider: {"env": env, "configured": bool(os.getenv(env, "").strip())}
        for provider, env in ENV_KEYS.items()
    }
