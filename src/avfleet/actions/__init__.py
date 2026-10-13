"""Self-healing actions. Importing this package registers the built-in actions."""

from . import roon_restart  # noqa: F401
from .base import Action, build_action, register_action

__all__ = ["Action", "build_action", "register_action"]
