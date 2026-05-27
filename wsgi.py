"""WSGI entrypoint for gunicorn in production."""
from __init__ import create_app

app = create_app()
