"""The single place that locates and loads the raw agent_config.json.

agent_config.json is the single source of truth for the whole app; this
module is the one place that decides WHERE that file lives and reads it as a
raw dict. APP_CONFIG_PATH (if set and pointing at an existing file) wins;
otherwise the internal src/config/agent_config.json is used.

Every ad-hoc reader across the codebase (agent card, tool configs, request
context, the env bridge) previously re-implemented this same resolve-then-load
dance with subtly different fallback semantics. They now share these two
functions instead.

Imports only the stdlib, so any module can use it without the circular import
that pulling in `settings` (which instantiates Settings() at import) would
cause.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

_INTERNAL_CONFIG_PATH = Path(__file__).parent / "agent_config.json"


def resolve_config_path() -> Path:
    """Return APP_CONFIG_PATH if it is set and the file exists, else the
    internal src/config/agent_config.json. This is the one fallback rule —
    an external path that does not exist falls back rather than failing."""
    ext = os.getenv("APP_CONFIG_PATH")
    if ext and Path(ext).exists():
        return Path(ext)
    return _INTERNAL_CONFIG_PATH


def load_raw_config() -> Dict[str, Any]:
    """Load the resolved agent_config.json as a raw dict. Returns {} on any
    failure (missing/malformed file), so callers can safely `.get(...)`."""
    try:
        with open(resolve_config_path()) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}
