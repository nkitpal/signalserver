"""ASGI entry point (``uvicorn signalserver_gui.api.asgi:app``)."""

from .app import create_app

app = create_app()
