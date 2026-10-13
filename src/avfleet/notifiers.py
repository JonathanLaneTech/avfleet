"""Alert delivery. Each notifier is configured by a [[notifiers]] table with a ``type``."""

from __future__ import annotations

import logging
import urllib.parse
from typing import Any

from .checks.base import http_request

log = logging.getLogger("avfleet.notify")


class Notifier:
    def send(self, title: str, message: str, priority: int = 0) -> bool:  # pragma: no cover
        raise NotImplementedError


class LogNotifier(Notifier):
    def send(self, title, message, priority=0):
        log.warning("ALERT %s: %s", title, message)
        return True


class PushoverNotifier(Notifier):
    URL = "https://api.pushover.net/1/messages.json"

    def __init__(self, token: str, user: str, url: str | None = None):
        if not token or not user:
            raise ValueError("pushover notifier needs token and user (set PUSHOVER_TOKEN / PUSHOVER_USER)")
        self.token, self.user, self.url = token, user, url or self.URL

    def send(self, title, message, priority=0):
        data = urllib.parse.urlencode({"token": self.token, "user": self.user, "title": title,
                                       "message": message, "priority": priority}).encode()
        try:
            resp = http_request(self.url, 10, method="POST", data=data)
        except OSError as exc:
            log.error("pushover send failed: %s", exc)
            return False
        if resp.status != 200:
            log.error("pushover HTTP %s: %s", resp.status, resp.body[:200])
        return resp.status == 200


def build_notifiers(configs: list[dict[str, Any]]) -> list[Notifier]:
    out: list[Notifier] = [LogNotifier()]
    for cfg in configs:
        kind = cfg.get("type")
        if kind == "pushover":
            try:
                out.append(PushoverNotifier(cfg.get("token", ""), cfg.get("user", ""), cfg.get("url")))
            except ValueError as exc:
                log.error("%s", exc)
        elif kind == "log":
            continue
        else:
            log.error("unknown notifier type %r", kind)
    return out
