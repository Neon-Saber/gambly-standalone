"""Production entrypoint, e.g.  gunicorn -w 2 -b 0.0.0.0:$PORT wsgi:app"""
from dashboard.app import create_app

app = create_app()
