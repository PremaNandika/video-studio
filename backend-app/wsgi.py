"""WSGI entry point.

Run with:  .venv/Scripts/python wsgi.py
or:        flask --app wsgi run --host=127.0.0.1 --port=5181
"""
from app import create_app

app = create_app()


if __name__ == "__main__":
    app.run(
        host=app.config["HOST"],
        port=app.config["PORT"],
        debug=False,
    )
