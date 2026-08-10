from __future__ import annotations

import getpass
import json
import os
import stat
from collections.abc import MutableMapping
from pathlib import Path

from .config import ENV_KEYS


_MAX_SECRETS_FILE_BYTES = 1_000_000
_SECRET_ENV_NAMES = frozenset(ENV_KEYS.values())


def secrets_path() -> Path:
    return Path.home() / ".config" / "searchx" / "secrets.env"


def _parse_dotenv_value(raw_value: str) -> str | None:
    """Parse the small, deliberately non-executing dotenv subset we support."""
    value = raw_value.strip()
    if value.startswith('"'):
        try:
            parsed, closing_index = json.JSONDecoder().raw_decode(value)
        except json.JSONDecodeError:
            return None
        trailing = value[closing_index:].strip()
        if trailing and not trailing.startswith("#"):
            return None
        return parsed if isinstance(parsed, str) else None
    if value.startswith("'"):
        quote = value[0]
        closing_index = value.find(quote, 1)
        if closing_index < 0:
            return None
        trailing = value[closing_index + 1 :].strip()
        if trailing and not trailing.startswith("#"):
            return None
        return value[1:closing_index]
    # Match the common ``KEY=value # comment`` form without interpreting
    # variable substitutions, escapes, or shell syntax.
    return value.split(" #", 1)[0].rstrip()


def load_secrets(
    path: Path | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> set[str]:
    """Load known SearchX credentials from the local dotenv-style file.

    Only known provider environment names are accepted.  Shell expansion is
    never performed, malformed lines are ignored, and the presence of a
    process environment variable (including an explicitly empty one) wins over
    the file value.
    """
    target = path or secrets_path()
    target_environ = os.environ if environ is None else environ
    try:
        if not target.is_file() or target.stat().st_size > _MAX_SECRETS_FILE_BYTES:
            return set()
        content = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return set()

    loaded: set[str] = set()
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, raw_value = line.partition("=")
        name = name.strip()
        if not separator or name not in _SECRET_ENV_NAMES or name in target_environ:
            continue
        value = _parse_dotenv_value(raw_value)
        if value is None or "\0" in value:
            continue
        try:
            target_environ[name] = value
        except Exception:
            continue
        loaded.add(name)
    return loaded


def secrets_metadata(path: Path | None = None) -> dict[str, object]:
    """Return non-sensitive local-file metadata suitable for ``doctor``."""
    target = path or secrets_path()
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except OSError:
        return {"exists": False}
    return {
        "exists": True,
        "permissions": f"{mode:04o}",
        "private": not bool(mode & (stat.S_IRWXG | stat.S_IRWXO)),
    }


def configure_secrets() -> Path:
    """Interactively save local secrets without echoing input."""
    path = secrets_path()
    path.parent.mkdir(mode=stat.S_IRWXU, parents=True, exist_ok=True)
    try:
        path.parent.chmod(stat.S_IRWXU)
    except OSError:
        pass
    values: dict[str, str] = {}
    for provider, env_name in ENV_KEYS.items():
        value = getpass.getpass(f"{provider} credential: ")
        if value:
            if "\n" in value or "\r" in value:
                raise ValueError("secret values must not contain newlines")
            if "\0" in value:
                raise ValueError("secret values must not contain NUL characters")
            values[env_name] = value

    content = "# SearchX local secrets. Do not commit.\n"
    content += "\n".join(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in values.items())
    content += "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write(content)
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return path
