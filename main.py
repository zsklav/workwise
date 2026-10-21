"""Compatibility entry point for the existing ``uvicorn main:app`` command."""
from workwise.api.main import app

__all__ = ["app"]
