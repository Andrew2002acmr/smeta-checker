"""The Codex audit server switch. Runtime credentials belong to the host worker."""

import os


def codex_audit_enabled() -> bool:
    return os.environ.get("CODEX_AUDIT_ENABLED", "").strip().lower() in {"1", "true"}
