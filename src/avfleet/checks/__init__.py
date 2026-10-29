"""Device checks. Importing this package registers every built-in check type."""

from . import airplay, generic, kaleidescape, lutron, roon, savant, tv, unifi, yamaha  # noqa: F401
from .base import Check, build_check, check_types, register

__all__ = ["Check", "build_check", "check_types", "register"]
