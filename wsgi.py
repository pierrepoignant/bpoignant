"""WSGI entrypoint for gunicorn in production."""
import os
from __init__ import create_app

db_name = os.environ.get('DB_NAME', 'ovh')
app = create_app(db_name)
