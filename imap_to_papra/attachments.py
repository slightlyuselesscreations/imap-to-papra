"""Decide which MIME parts are real attachments, and give them safe filenames.

Real-world mail is hostile here: signature logos masquerade as attachments,
S/MIME blobs ride along on every message, and filenames arrive RFC 2047-encoded,
absent, duplicated, or containing path separators.
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
from dataclasses import dataclass
from email.message import Message
from typing import Iterator

from imap_to_papra.config import AttachmentsConfig

# Characters that must never reach a filename: control chars plus the union of
# path separators and shell/Windows-reserved punctuation.
_UNSAFE_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')
_CID_REFERENCE = re.compile(r"""cid:([^"'\s>)]+)""", re.IGNORECASE)

MAX_FILENAME_LENGTH = 200


@dataclass(frozen=True)
class Attachment:
    """One attachment extracted from a message, ready to upload."""

    filename: str
    content_type: str
    payload: bytes

    @property
    def size(self) -> int:
        return len(self.payload)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()


@dataclass(frozen=True)
class Skipped:
    """A part that looked like an attachment but was not uploaded.

    `blocking` separates two very different situations. Deliberate filtering
    (deny list, inline logo, below min_size_bytes) is the configuration working
    as intended, and the message may still be deleted. A part we *wanted* but
    could not handle (oversized, undecodable) is blocking: deleting the mail
    would destroy the only copy of that attachment.
    """

    filename: str
    reason: str
    blocking: bool = False


@dataclass(frozen=True)
class Selection:
    attachments: list[Attachment]
    skipped: list[Skipped]

    @property
    def blocking_skips(self) -> list[Skipped]:
        return [item for item in self.skipped if item.blocking]


def _leaf_parts(message: Message) -> Iterator[Message]:
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        yield part


def _cid_references(message: Message) -> set[str]:
    """Content-IDs referenced by `cid:` URLs in the HTML body parts."""
    references: set[str] = set()
    for part in _leaf_parts(message):
        if part.get_content_type() != "text/html":
            continue
        try:
            body = part.get_payload(decode=True)
        except Exception:  # pragma: no cover - defensive against malformed MIME
            continue
        if not body:
            continue
        text = body.decode(part.get_content_charset() or "utf-8", errors="replace")
        for match in _CID_REFERENCE.finditer(text):
            references.add(match.group(1).strip().strip("\"'").lower())
    return references


def _content_id(part: Message) -> str:
    raw = part.get("Content-ID", "") or ""
    return raw.strip().strip("<>").lower()


def _payload_bytes(part: Message) -> bytes:
    """Raw decoded bytes for a part, including forwarded messages."""
    if part.get_content_type() == "message/rfc822":
        # get_payload(decode=True) returns None for message/* parts, so
        # re-serialise the embedded message instead.
        nested = part.get_payload()
        if isinstance(nested, list) and nested:
            return nested[0].as_bytes()
    data = part.get_payload(decode=True)
    return data if isinstance(data, bytes) else b""


def extension_of(filename: str) -> str:
    _, _, suffix = filename.rpartition(".")
    return suffix.lower() if suffix and suffix != filename else ""


def sanitise_filename(raw: str, *, fallback_stem: str, index: int, content_type: str) -> str:
    """Turn a mail-supplied filename into something safe and non-empty.

    The name is used as the Papra document name and as a multipart form field
    value; it must never be interpretable as a path.
    """
    name = (raw or "").strip()

    # Discard any directory component: only the final segment can survive.
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE_CHARS.sub("_", name)
    # Leading/trailing dots and spaces cover "..", "." and Windows oddities.
    name = name.strip(". ").strip()

    if not name:
        suffix = mimetypes.guess_extension(content_type) or ".bin"
        name = f"{fallback_stem}-{index}{suffix}"

    if len(name) > MAX_FILENAME_LENGTH:
        stem, dot, suffix = name.rpartition(".")
        if dot and len(suffix) <= 10:
            keep = MAX_FILENAME_LENGTH - len(suffix) - 1
            name = f"{stem[:keep]}.{suffix}"
        else:
            name = name[:MAX_FILENAME_LENGTH]

    return name


def _deduplicate(name: str, seen: set[str]) -> str:
    """Make `name` unique within a single message."""
    if name not in seen:
        seen.add(name)
        return name

    stem, dot, suffix = name.rpartition(".")
    if not dot:
        stem, suffix = name, ""

    counter = 1
    while True:
        candidate = f"{stem}-{counter}.{suffix}" if suffix else f"{stem}-{counter}"
        if candidate not in seen:
            seen.add(candidate)
            return candidate
        counter += 1


def _is_candidate(part: Message, cid_refs: set[str], cfg: AttachmentsConfig) -> tuple[bool, str]:
    """Decide whether a leaf part counts as an attachment.

    Returns (is_candidate, skip_reason). A part that is plainly a message body
    is not a candidate at all and produces no skip record.
    """
    disposition = (part.get_content_disposition() or "").lower()
    has_filename = bool(part.get_filename())

    # A part the HTML body pulls in via cid: is part of that body, whatever its
    # Content-Disposition claims. Plenty of clients label embedded signature
    # logos as "attachment", so this check has to come first or those logos end
    # up archived as documents.
    if cfg.skip_inline:
        content_id = _content_id(part)
        if content_id and content_id in cid_refs:
            return False, "referenced inline by cid:"

    if disposition == "attachment":
        return True, ""

    if not has_filename:
        return False, ""

    if cfg.skip_inline and disposition == "inline":
        return False, "inline disposition"

    return True, ""


def select(message: Message, cfg: AttachmentsConfig, *, fallback_stem: str = "attachment") -> Selection:
    """Extract the attachments of `message` that pass the configured filters."""
    cid_refs = _cid_references(message) if cfg.skip_inline else set()
    attachments: list[Attachment] = []
    skipped: list[Skipped] = []
    seen_names: set[str] = set()

    for index, part in enumerate(_leaf_parts(message)):
        is_candidate, reason = _is_candidate(part, cid_refs, cfg)
        raw_name = part.get_filename() or ""

        if not is_candidate:
            if reason:
                skipped.append(Skipped(raw_name or "<unnamed>", reason))
            continue

        content_type = part.get_content_type()
        name = sanitise_filename(raw_name, fallback_stem=fallback_stem, index=index, content_type=content_type)
        extension = extension_of(name)

        if extension in cfg.denied:
            skipped.append(Skipped(name, f"extension {extension!r} is denied"))
            continue

        if cfg.allowed and extension not in cfg.allowed:
            skipped.append(Skipped(name, f"extension {extension!r} is not in the allow list"))
            continue

        payload = _payload_bytes(part)
        if not payload:
            skipped.append(Skipped(name, "empty or undecodable payload", blocking=True))
            continue

        if len(payload) < cfg.min_size_bytes:
            skipped.append(Skipped(name, f"{len(payload)} bytes is below min_size_bytes ({cfg.min_size_bytes})"))
            continue

        if len(payload) > cfg.max_size_bytes:
            skipped.append(
                Skipped(
                    name,
                    f"{len(payload)} bytes exceeds max_size_bytes ({cfg.max_size_bytes})",
                    blocking=True,
                )
            )
            continue

        attachments.append(
            Attachment(
                filename=_deduplicate(name, seen_names),
                content_type=content_type,
                payload=payload,
            )
        )

    return Selection(attachments=attachments, skipped=skipped)
