"""Config bootstrap: env-var → app.config loader.

Same shape as the `seo/` project: every secret comes from an env var named
`<SECTION>__<KEY>` (double underscore separator). Sections are registered in
`env_loader.ENV_VAR_KEYS`.
"""

from .env_loader import get_env_var, ENV_VAR_KEYS


def initialize_config(app):
    for section, keys in ENV_VAR_KEYS.items():
        if section not in app.config:
            app.config[section] = {}
        for key in keys:
            env_val = get_env_var(section, key)
            if env_val is not None:
                app.config[section][key] = env_val
