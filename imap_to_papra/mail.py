"""IMAP session handling: fetch unread mail, then dispose of what we archived."""

from __future__ import annotations

import email
import logging
import ssl
from contextlib import contextmanager
from email.message import Message
from email.policy import default as default_policy
from typing import Iterator

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError, LoginError

from imap_to_papra.config import ImapConfig

log = logging.getLogger(__name__)

# Servers answer a BODY.PEEK[] request with a BODY[] key; RFC822 is the
# pre-IMAP4rev1 spelling some servers still use.
_BODY_KEYS = (b"BODY[]", b"RFC822")


class MailboxError(Exception):
    """Raised for IMAP connection, authentication or protocol failures."""


def _ssl_context(verify: bool) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if not verify:
        log.warning("IMAP certificate verification is disabled (imap.verify_ssl = false)")
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


@contextmanager
def connect(cfg: ImapConfig) -> Iterator["Mailbox"]:
    """Open a mailbox, guaranteeing logout even when the caller raises."""
    context = _ssl_context(cfg.verify_ssl)

    try:
        client = IMAPClient(cfg.host, port=cfg.port, ssl=cfg.ssl, ssl_context=context if cfg.ssl else None, timeout=cfg.timeout_seconds)
    except (OSError, IMAPClientError) as exc:
        raise MailboxError(f"cannot connect to {cfg.host}:{cfg.port}: {exc}") from exc

    try:
        if cfg.starttls:
            client.starttls(ssl_context=context)
        try:
            client.login(cfg.username, cfg.password)
        except LoginError as exc:
            raise MailboxError(f"IMAP login failed for {cfg.username}: {exc}") from exc

        try:
            client.select_folder(cfg.mailbox)
        except IMAPClientError as exc:
            raise MailboxError(f"cannot open mailbox {cfg.mailbox!r}: {exc}") from exc

        yield Mailbox(client, cfg)
    except (OSError, IMAPClientError) as exc:
        raise MailboxError(f"IMAP error: {exc}") from exc
    finally:
        try:
            client.logout()
        except Exception:  # pragma: no cover - a failed logout must not mask real errors
            pass


class Mailbox:
    """Operations on the selected folder."""

    def __init__(self, client: IMAPClient, cfg: ImapConfig) -> None:
        self._client = client
        self._cfg = cfg

    def unread_uids(self) -> list[int]:
        """UIDs of unread messages, oldest first, capped by batch_size."""
        uids = sorted(self._client.search(["UNSEEN"]))
        if self._cfg.batch_size:
            return uids[: self._cfg.batch_size]
        return uids

    def fetch_message(self, uid: int) -> Message:
        """Download one message without marking it read.

        BODY.PEEK is load-bearing: a plain BODY[] fetch sets \\Seen as a side
        effect, which would erase the only marker distinguishing processed mail
        from unprocessed mail if the run then failed.
        """
        response = self._client.fetch([uid], ["BODY.PEEK[]"])
        data = response.get(uid)
        if not data:
            raise MailboxError(f"message {uid} disappeared before it could be fetched")

        for key in _BODY_KEYS:
            raw = data.get(key)
            if raw:
                return email.message_from_bytes(raw, policy=default_policy)

        raise MailboxError(f"message {uid}: server returned no body (keys: {sorted(data)})")

    def mark_read(self, uids: list[int]) -> None:
        if uids:
            self._client.add_flags(uids, [b"\\Seen"])

    def dispose(self, uids: list[int]) -> None:
        """Apply the configured on_success action to fully archived messages."""
        if not uids:
            return

        action = self._cfg.on_success
        if action == "mark_read":
            self.mark_read(uids)
        elif action == "move":
            self._move(uids)
        elif action == "delete":
            self._client.delete_messages(uids)
        else:  # pragma: no cover - config validation prevents this
            raise MailboxError(f"unknown on_success action {action!r}")

    def _move(self, uids: list[int]) -> None:
        folder = self._cfg.move_to
        if not self._client.folder_exists(folder):
            log.info("creating IMAP folder %r", folder)
            self._client.create_folder(folder)

        if self._client.has_capability("MOVE"):
            self._client.move(uids, folder)
        else:
            # Older servers: copy, then flag for the expunge at end of run.
            self._client.copy(uids, folder)
            self._client.delete_messages(uids)

    def finalise(self) -> None:
        """Expunge once per run rather than once per message."""
        if self._cfg.on_success == "mark_read":
            return
        try:
            self._client.expunge()
        except IMAPClientError as exc:
            raise MailboxError(f"expunge failed: {exc}") from exc
