"""Local HTTP/SSE API (stdlib WSGI, no third-party server required)."""

from usbdoctor.webapi.server import ApiApp, create_app, serve

__all__ = ["ApiApp", "create_app", "serve"]
