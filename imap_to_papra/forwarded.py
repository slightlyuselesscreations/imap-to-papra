"""Recover the headers of the message that was forwarded to us.

Forwarding rewrites From to whoever forwarded it, so an invoice a colleague
passes on would be filed under the colleague rather than the company that sent
it. Two shapes reach the intake mailbox:

  * forward as attachment - the original rides along as a message/rfc822 part
    and its real headers can be read straight off it. Reliable.
  * ordinary forward - the client pastes the original headers into the top of
    the body as text. Recoverable only as far as the client's own wording goes,
    which is why the label lists below are not just English.

Only From and Subject come back dependably. Clients localise and reformat the
date line ("Sent: Tuesday, August 25, 2026 9:00 AM", or Gmail's "Date: Tue, 25
Aug 2026 at 09:00"), so a date that will not parse is left to the caller to
fall back on. Nothing here guesses.
"""

from __future__ import annotations

import logging
import re
from email.message import Message
from html import unescape

log = logging.getLogger(__name__)

# The header labels clients write when they paste a forwarded block, in the
# languages this is likely to meet. Matching is case-folded.
_FROM_LABELS = frozenset({"from", "von", "da", "de", "van", "fra", "från", "od", "nadawca"})
_SUBJECT_LABELS = frozenset({"subject", "betreff", "oggetto", "objet", "asunto", "assunto",
                             "onderwerp", "ämne", "emne", "temat"})
_DATE_LABELS = frozenset({"date", "sent", "datum", "gesendet", "data", "inviato", "fecha",
                          "enviado", "envoyé", "verzonden", "skickat", "wysłano"})

# A line that announces a forwarded block. Scanning starts after one of these
# when present, which keeps a quoted reply chain further down out of the way.
_MARKERS = (
    "forwarded message",
    "original message",
    "begin forwarded message",
    "weitergeleitete nachricht",
    "ursprüngliche nachricht",
    "messaggio inoltrato",
    "messaggio originale",
    "message transféré",
    "message d'origine",
    "mensaje reenviado",
    "mensaje original",
    "doorgestuurd bericht",
)

_ADDRESS = re.compile(r"[^\s<>@,;:\"]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
# Bullets, quote markers and leftover emphasis that clients wrap labels in.
_LABEL_NOISE = "*_> \t "
# How far past the From line the rest of the block can reasonably be.
_BLOCK_LINES = 12


def original_headers(message: Message) -> dict[str, str]:
    """From/Subject/Date of the forwarded original, or {} if there is none.

    Keys are only present when found, and values are single-line and raw - the
    caller parses the address and the date, exactly as it does for a message
    that was not forwarded.
    """
    return _from_attached_message(message) or _from_body_text(message)


def _flatten(value: object) -> str:
    return str(value or "").replace("\n", " ").replace("\r", " ").strip()


def _from_attached_message(message: Message) -> dict[str, str]:
    """Headers of an embedded message/rfc822 part, the trustworthy case."""
    for part in message.walk():
        if part.get_content_type() != "message/rfc822":
            continue

        payload = part.get_payload()
        if not isinstance(payload, list) or not payload:
            continue
        inner = payload[0]
        if not isinstance(inner, Message):
            continue

        found = {
            key: _flatten(inner.get(header))
            for key, header in (("from", "From"), ("subject", "Subject"), ("date", "Date"))
        }
        if found["from"]:
            log.debug("original headers read from the attached message")
            return {key: value for key, value in found.items() if value}

    return {}


def _body_text(message: Message) -> str:
    """The first text body of the message, HTML flattened if that is all there is."""
    plain = html = ""
    for part in message.walk():
        content_type = part.get_content_type()
        if content_type not in ("text/plain", "text/html"):
            continue
        if part.get_content_disposition() == "attachment":
            continue

        data = part.get_payload(decode=True)
        if not isinstance(data, bytes) or not data:
            continue
        text = data.decode(part.get_content_charset() or "utf-8", errors="replace")

        if content_type == "text/plain" and not plain:
            plain = text
        elif content_type == "text/html" and not html:
            html = text

    return plain or (_strip_tags(html) if html else "")


def _strip_tags(html: str) -> str:
    """Crude tag removal, enough to get a pasted header block onto its own lines."""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?i)<(br|/p|/div|/tr|/li|/h[1-6])\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return unescape(text)


def _labelled(line: str) -> tuple[str, str]:
    """Split "From: Acme <a@b.com>" into ("from", "Acme <a@b.com>")."""
    label, separator, value = line.partition(":")
    if not separator:
        return "", ""
    return label.strip(_LABEL_NOISE).casefold(), value.strip()


def _marker_index(lines: list[str]) -> int:
    for index, line in enumerate(lines):
        folded = line.casefold()
        if any(marker in folded for marker in _MARKERS):
            return index + 1
    return 0


def _from_body_text(message: Message) -> dict[str, str]:
    """Headers out of the block a client pastes at the top of a forward."""
    lines = _body_text(message).splitlines()
    if not lines:
        return {}

    start = _marker_index(lines)
    for index in range(start, len(lines)):
        label, value = _labelled(lines[index])
        if label not in _FROM_LABELS or not _ADDRESS.search(value):
            continue

        found = {"from": value}
        for line in lines[index + 1:index + 1 + _BLOCK_LINES]:
            other_label, other_value = _labelled(line)
            if not other_value:
                continue
            if other_label in _SUBJECT_LABELS and "subject" not in found:
                found["subject"] = other_value
            elif other_label in _DATE_LABELS and "date" not in found:
                found["date"] = other_value

        log.debug("original headers read from the forwarded block in the body")
        return found

    return {}


__all__ = ["original_headers"]
