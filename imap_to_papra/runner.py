"""One pass: drain unread mail into Papra, then dispose of what was archived."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from email.message import Message
from email.utils import parsedate_to_datetime, parseaddr

from imap_to_papra import attachments as attachments_mod
from imap_to_papra import forwarded, mail, notify
from imap_to_papra.attachments import Attachment, Selection
from imap_to_papra.config import Config
from imap_to_papra.papra import PapraAuthError, PapraClient, PapraError, PapraUploadError

log = logging.getLogger(__name__)

_UNSAFE_STEM = re.compile(r"[^A-Za-z0-9._-]+")

# ntfy's default maximum message length is 4096 bytes; cap well below it so a
# large batch degrades into a readable digest rather than a truncated one.
MAX_NOTIFIED_MESSAGES = 10


@dataclass
class ArchivedMessage:
    """What was archived, and which mail it came from.

    Kept so the ntfy notification can name the document, the sender and the
    subject rather than just reporting a count.
    """

    sender: str
    subject: str
    documents: list[str] = field(default_factory=list)

    def render(self) -> str:
        names = ", ".join(self.documents) or "(no documents)"
        return f"{names}\nFrom: {self.sender}\nSubject: {self.subject}"


@dataclass
class Summary:
    """Counters for one pass, used for logging, notifications and exit status."""

    messages_seen: int = 0
    messages_archived: int = 0
    messages_skipped: int = 0
    messages_failed: int = 0
    uploaded: int = 0
    deduplicated: int = 0
    attachments_skipped: int = 0
    archived: list[ArchivedMessage] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def document_count(self) -> int:
        return sum(len(item.documents) for item in self.archived)

    @property
    def ok(self) -> bool:
        return not self.errors and self.messages_failed == 0

    @property
    def did_something(self) -> bool:
        return self.uploaded > 0 or self.deduplicated > 0

    def line(self) -> str:
        return (
            f"messages_seen={self.messages_seen} archived={self.messages_archived} "
            f"skipped={self.messages_skipped} failed={self.messages_failed} "
            f"uploaded={self.uploaded} deduplicated={self.deduplicated} "
            f"attachments_skipped={self.attachments_skipped}"
        )


def _fallback_stem(message: Message) -> str:
    """A stable, filesystem-safe stem for attachments that arrive unnamed."""
    raw = str(message.get("Message-ID", "") or "").strip().strip("<>")
    stem = _UNSAFE_STEM.sub("-", raw).strip("-")
    return stem[:60] or "attachment"


EMAIL_PROPERTIES: dict[str, str] = {
    "Email subject": "text",
    "Email sender": "text",
    "Email import": "boolean",
    "Email date": "date",
    "Attachment filename": "text",
}


def _header(message: Message, name: str) -> str:
    """A header flattened to a single line, safe to put in a notification."""
    return str(message.get(name, "") or "").replace("\n", " ").replace("\r", " ").strip()


def subject_of(message: Message) -> str:
    return _header(message, "Subject") or "(no subject)"


def sender_of(message: Message) -> str:
    """Just the address part of From, so notifications stay short."""
    raw = _header(message, "From")
    _display_name, address = parseaddr(raw)
    return address or raw or "(unknown sender)"


def _iso_date(raw: str) -> str:
    """An RFC 2822 date as ISO 8601, which is what Papra's date type accepts."""
    if not raw:
        return ""
    try:
        return parsedate_to_datetime(raw).isoformat()
    except (TypeError, ValueError):
        log.debug("  unparseable date %r; falling back", raw)
        return ""


@dataclass(frozen=True)
class MailFacts:
    """Who really sent a message, and about what.

    A forwarded mail's own From is whoever forwarded it, which files the
    document under the wrong person. When the original is recoverable its
    headers win, so both a forward and the mail it carried land the same way.

    Fields are empty rather than placeholders when a header is absent: these
    feed custom property values, where an unset property is the honest answer.
    """

    sender: str
    subject: str
    sent_at: str
    forwarded: bool

    @classmethod
    def of(cls, message: Message) -> "MailFacts":
        original = forwarded.original_headers(message)
        # The original's date is the one clients mangle most, so an unparseable
        # one falls back to the forward's own date rather than being dropped.
        return cls(
            sender=parseaddr(original.get("from") or _header(message, "From"))[1],
            subject=original.get("subject") or _header(message, "Subject"),
            sent_at=_iso_date(original.get("date", "")) or _iso_date(_header(message, "Date")),
            forwarded=bool(original),
        )

    def describe(self) -> str:
        return f"{self.subject or '(no subject)'} from {self.sender or '(unknown sender)'}"


def _property_values(facts: MailFacts, attachment: Attachment) -> dict[str, object]:
    """The custom property values for one document.

    A forward-as-attachment archives the carried mail itself as well as what was
    inside it. That wrapper has no attachment filename worth recording: names
    like "Original.eml" are invented by the forwarding client and say nothing
    about the document, so the property is left unset rather than filled with
    noise.
    """
    values: dict[str, object] = {
        "Email subject": facts.subject,
        "Email sender": facts.sender,
        "Email import": True,
        "Email date": facts.sent_at,
        "Attachment filename": "" if _is_carried_mail(attachment) else attachment.filename,
    }
    return {name: value for name, value in values.items() if value != ""}


def _is_carried_mail(attachment: Attachment) -> bool:
    return attachment.content_type == "message/rfc822"


def _resolve_properties(client: PapraClient) -> dict[str, str]:
    """Look up (and create) this tool's custom properties, once per run.

    A key without the custom-properties permissions still archives mail, so a
    failure here is a warning and an empty map, not the end of the run.
    """
    try:
        return client.property_definitions(EMAIL_PROPERTIES)
    except PapraError as exc:
        log.warning("custom properties unavailable, documents will be filed without them: %s", exc)
        return {}


def _apply_properties(
    client: PapraClient,
    document_id: str,
    values: dict[str, object],
    definitions: dict[str, str],
) -> None:
    """Label a stored document, tolerating failure.

    The document is uploaded and verified by this point. Losing a label is
    cosmetic; refusing to delete the mail over it would leave the mailbox
    filling up and the attachment re-uploaded on every run.
    """
    for name, value in values.items():
        definition_id = definitions.get(name)
        if not definition_id:
            continue
        try:
            client.set_property(document_id, definition_id, value)
        except PapraError as exc:
            log.warning("  could not set %r on document %s: %s", name, document_id, exc)


def _log_skips(selection: Selection) -> None:
    for skip in selection.skipped:
        if skip.blocking:
            log.warning("  not archived: %s (%s)", skip.filename, skip.reason)
        else:
            log.debug("  filtered out: %s (%s)", skip.filename, skip.reason)


def _process_message(
    client: PapraClient,
    facts: MailFacts,
    selection: Selection,
    summary: Summary,
    definitions: dict[str, str],
    *,
    dry_run: bool,
) -> tuple[bool, list[str]]:
    """Upload and verify every attachment.

    Returns (message may be disposed of, names of the documents now in Papra).
    """
    all_ok = True
    stored: list[str] = []

    for attachment in selection.attachments:
        if dry_run:
            log.info(
                "  would upload %s (%s, %d bytes, sha256 %s…)",
                attachment.filename,
                attachment.content_type,
                attachment.size,
                attachment.sha256[:12],
            )
            log.debug("  would label it %s", _property_values(facts, attachment))
            continue

        try:
            result = client.upload(attachment)
            if result.document_id:
                client.verify(result.document_id, attachment)
                _apply_properties(
                    client,
                    result.document_id,
                    _property_values(facts, attachment),
                    definitions,
                )
        except PapraUploadError as exc:
            log.error("  %s", exc)
            summary.errors.append(str(exc))
            all_ok = False
            continue

        if result.deduplicated:
            summary.deduplicated += 1
            stored.append(f"{attachment.filename} (already in Papra)")
        else:
            summary.uploaded += 1
            stored.append(attachment.filename)
        log.info("  %s", result.summary)

    return all_ok, stored


def run_once(cfg: Config, *, dry_run: bool = False) -> Summary:
    """Execute a single pass and return its summary."""
    summary = Summary()
    attachments_mod.log_type_sources()

    with PapraClient(cfg.papra) as client:
        info = client.preflight()
        log.info(
            "Papra reachable at %s (API key: %s)",
            cfg.papra.base_url,
            info.get("name") or info.get("id") or "unnamed",
        )

        # A dry run must not create anything, property definitions included.
        definitions = {} if dry_run else _resolve_properties(client)

        with mail.connect(cfg.imap) as mailbox:
            uids = mailbox.unread_uids()
            summary.messages_seen = len(uids)

            if not uids:
                log.info("no unread messages in %s", cfg.imap.mailbox)
                return summary

            log.info("found %d unread message(s) in %s", len(uids), cfg.imap.mailbox)
            disposable: list[int] = []

            for uid in uids:
                message = mailbox.fetch_message(uid)
                facts = MailFacts.of(message)
                log.info("message %d: %s", uid, facts.describe())
                if facts.forwarded:
                    log.info("  forwarded mail: filing it under the original sender")

                selection = attachments_mod.select(
                    message,
                    cfg.attachments,
                    fallback_stem=_fallback_stem(message),
                )
                _log_skips(selection)
                summary.attachments_skipped += len(selection.skipped)

                blocking = selection.blocking_skips
                if blocking:
                    # Something we could not handle lives in this mail; deleting
                    # it would destroy the only copy.
                    reasons = "; ".join(f"{item.filename} ({item.reason})" for item in blocking)
                    log.error("  leaving message %d in place: %s", uid, reasons)
                    summary.errors.append(f"message {uid}: {reasons}")
                    summary.messages_failed += 1
                    continue

                if not selection.attachments:
                    log.info("  no qualifying attachments — leaving message untouched and unread")
                    summary.messages_skipped += 1
                    continue

                archived_ok, stored = _process_message(
                    client, facts, selection, summary, definitions, dry_run=dry_run
                )
                if archived_ok:
                    summary.messages_archived += 1
                    if stored:
                        summary.archived.append(
                            ArchivedMessage(
                                sender=facts.sender or "(unknown sender)",
                                subject=facts.subject or "(no subject)",
                                documents=stored,
                            )
                        )
                    if not dry_run:
                        disposable.append(uid)
                else:
                    summary.messages_failed += 1
                    log.error("  message %d left unread; it will be retried on the next run", uid)

            if dry_run:
                log.info("dry run: no uploads performed and no messages modified")
            elif disposable:
                log.info("applying on_success=%s to %d message(s)", cfg.imap.on_success, len(disposable))
                mailbox.dispose(disposable)
                mailbox.finalise()

    return summary


def notify_result(cfg: Config, summary: Summary) -> None:
    """Send the ntfy notification appropriate to this pass, if any."""
    if summary.errors or summary.messages_failed:
        detail = "\n".join(summary.errors[:10]) or "see logs for details"
        notify.send(
            cfg.ntfy,
            event="error",
            title="imap-to-papra: run failed",
            body=f"{summary.line()}\n\n{detail}",
            tags="warning",
        )
        return

    if summary.did_something:
        notify.send(
            cfg.ntfy,
            event="success",
            title=success_title(summary),
            body=success_body(summary),
            tags="inbox_tray",
        )


def success_title(summary: Summary) -> str:
    count = summary.document_count
    return "1 document archived" if count == 1 else f"{count} documents archived"


def success_body(summary: Summary) -> str:
    """Name each document alongside the sender and subject it came from."""
    blocks = [item.render() for item in summary.archived[:MAX_NOTIFIED_MESSAGES]]

    remaining = len(summary.archived) - MAX_NOTIFIED_MESSAGES
    if remaining > 0:
        blocks.append(f"…and {remaining} more message(s)")

    return "\n\n".join(blocks)


def notify_failure(cfg: Config, message: str) -> None:
    """Notify about a failure that stopped the run before it could summarise."""
    notify.send(
        cfg.ntfy,
        event="error",
        title="imap-to-papra: run failed",
        body=message,
        tags="rotating_light",
    )


__all__ = [
    "Summary",
    "run_once",
    "notify_result",
    "notify_failure",
    "PapraAuthError",
    "PapraError",
]
