"""Best-effort ntfy notifications.

Notification is the least important thing this tool does. A dead or misconfigured
ntfy server must never turn a successful archival run into a failed one, so every
failure here is logged and swallowed.
"""

from __future__ import annotations

import logging

import requests

from imap_to_papra.config import NtfyConfig

log = logging.getLogger(__name__)


def send(cfg: NtfyConfig, *, event: str, title: str, body: str, tags: str = "") -> None:
    """Publish a notification if this event type is enabled."""
    if not cfg.wants(event):
        return

    headers = {
        "Title": title,
        "Priority": "high" if event == "error" else cfg.priority,
    }
    if tags:
        headers["Tags"] = tags
    if cfg.token:
        headers["Authorization"] = f"Bearer {cfg.token}"

    url = f"{cfg.server}/{cfg.topic}"
    try:
        response = requests.post(
            url,
            data=body.encode("utf-8"),
            headers=headers,
            timeout=cfg.timeout_seconds,
        )
        if not response.ok:
            log.warning("ntfy returned HTTP %d for %s", response.status_code, url)
        else:
            log.debug("ntfy notification sent to %s", url)
    except requests.RequestException as exc:
        log.warning("ntfy notification failed (continuing anyway): %s", exc)
