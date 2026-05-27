"""Env var loader.

Resolves config values from env vars using the convention `<SECTION>__<KEY>`.
For local dev, also loads `bpoignant.json` from the project root (if present)
so devs hit the same OVH MySQL as production without setting env vars by hand.

`bpoignant.json` is gitignored. Its keys are pushed into `os.environ` only if
the env var isn't already set — env always wins, file is the fallback.
"""

import json
import os
from typing import Optional


def _load_local_secrets_file():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, 'bpoignant.json')
    if not os.path.exists(path):
        return
    try:
        with open(path, 'r') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    for k, v in data.items():
        if k not in os.environ and v is not None:
            os.environ[k] = str(v)


_load_local_secrets_file()


def section_to_env_prefix(section: str) -> str:
    return section.replace('-', '_').upper()


def get_env_var(section: str, key: str) -> Optional[str]:
    prefix = section_to_env_prefix(section)
    return os.environ.get(f"{prefix}__{key.upper()}")


# Sections + the keys each one expects.
ENV_VAR_KEYS = {
    'database-ovh':   ['host', 'user', 'password', 'name', 'port'],
    'database-local': ['host', 'user', 'password', 'name', 'port'],
}
