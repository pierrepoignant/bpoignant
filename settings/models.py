"""Generic application config, stored in the database.

A simple key/value table for settings that shouldn't live in code or env vars
— currently the Google Drive OAuth credentials (client id / secret) and the
refresh token obtained through the admin "Connecter" flow.

The column names are `config_key` / `config_value` on purpose: `key` and
`value` are reserved words in MySQL, and spelling the columns out avoids any
quoting surprises across the SQLite (dev) / MySQL (prod) split.
"""

from datetime import datetime

from init_db import db


class Config(db.Model):
    __tablename__ = 'config'

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column('config_key', db.String(100), unique=True, nullable=False, index=True)
    value = db.Column('config_value', db.Text, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


def get_config(key, default=None):
    """Return the stored value for `key`, or `default` when unset/empty."""
    row = Config.query.filter_by(key=key).first()
    if row is None or row.value is None:
        return default
    return row.value


def set_config(key, value):
    """Insert or update `key`. A None/empty value is stored as-is (use
    `delete_config` to remove a key entirely)."""
    row = Config.query.filter_by(key=key).first()
    if row is None:
        row = Config(key=key, value=value)
        db.session.add(row)
    else:
        row.value = value
    db.session.commit()
    return row


def delete_config(key):
    """Remove `key` if present. No-op when it isn't."""
    Config.query.filter_by(key=key).delete()
    db.session.commit()
