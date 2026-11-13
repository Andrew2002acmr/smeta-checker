"""Project settings: .env file, process environment, injected secrets.

No UI dependency: callers pass secrets explicitly so the same
resolution works headless.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values


def _project_env_path() -> Path:
    return Path(__file__).resolve().parent.parent / ".env"


def get_setting(name: str, default: str = "", *, file_values=None, secrets=None) -> str:
    """Resolve ``name``: project .env, process environment, injected secrets.

    ``file_values`` defaults to a fresh read of the project ``.env`` on every
    call, so edits are picked up without a restart. ``secrets`` accepts
    anything with ``.get`` (a secrets mapping, a dict in tests);
    ``None`` skips the secrets fallback.
    """
    if file_values is None:
        file_values = dotenv_values(_project_env_path(), encoding="utf-8-sig")
    if name in file_values:
        value = file_values[name]
        return str(value).strip() if value else default

    value = os.getenv(name)
    if value:
        return value.strip()

    if secrets is None:
        return default
    try:
        value = secrets.get(name, default)
        return str(value).strip() if value else default
    except (FileNotFoundError, KeyError):
        return default
