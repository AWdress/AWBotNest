"""Compatibility import for the API app factory.

Endpoint implementations live under :mod:`awbotnest.api`; this module remains so existing
platform imports of ``awbotnest.app.create_app`` continue to work.
"""
from .api.app import create_app

__all__ = ["create_app"]
