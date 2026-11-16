"""Compatibility alias for :mod:`forgepilot.conversations.stream`."""
from importlib import import_module as _import_module
import sys as _sys

_sys.modules[__name__] = _import_module(".conversations.stream", __package__)
