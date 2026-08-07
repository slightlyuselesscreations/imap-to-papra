"""Command line entry point.

Every invocation performs exactly one pass and exits. There is deliberately no
loop or daemon mode: cadence belongs to cron, a systemd timer, or supercronic
inside the container.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from imap_to_papra import __version__, config as config_mod, locking, mail, runner
from imap_to_papra.config import Config, ConfigError
from imap_to_papra.papra import PapraAuthError, PapraClient, PapraError

EXIT_OK = 0
EXIT_PARTIAL_FAILURE = 1
EXIT_CONFIG_ERROR = 2
EXIT_CONNECTION_ERROR = 3

log = logging.getLogger("imap_to_papra")


class _JsonFormatter(logging.Formatter):
    """One JSON object per line, for log shippers that prefer structure."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _configure_logging(level: str, fmt: str) -> None:
    """Log to stdout only: cron mails it, journald indexes it, docker logs shows it."""
    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z"))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # imapclient is chatty at DEBUG and would dump message bodies.
    logging.getLogger("imapclient").setLevel(max(logging.INFO, getattr(logging, level, logging.INFO)))
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="imap-to-papra",
        description=(
            "Upload attachments from unread IMAP mail into a Papra instance, "
            "then dispose of the processed mail. Runs once and exits; schedule it "
            "with cron, a systemd timer, or supercronic."
        ),
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        help="path to config.toml (default: $IMAP_TO_PAPRA_CONFIG, ./config.toml, /etc/imap-to-papra/config.toml)",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="report what would be uploaded without uploading or modifying any mail",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify IMAP login and Papra credentials, then exit without touching mail",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        help="override logging.level from the config file",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def _load_config(explicit: Path | None) -> Config:
    path = explicit or config_mod.default_config_path()
    if path is None:
        raise ConfigError(
            "no config file found. Pass --config PATH, set IMAP_TO_PAPRA_CONFIG, "
            "or create ./config.toml (start from config.example.toml)."
        )
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")

    cfg = config_mod.load(path)
    log.debug("loaded configuration from %s", path)
    return cfg


def _run_check(cfg: Config) -> int:
    """Prove both ends are reachable and authorised, changing nothing."""
    with PapraClient(cfg.papra) as client:
        info = client.preflight()
    permissions = info.get("permissions")
    log.info(
        "Papra OK: %s (key %s, permissions: %s)",
        cfg.papra.base_url,
        info.get("name") or info.get("id") or "unnamed",
        ", ".join(permissions) if isinstance(permissions, list) else "not reported",
    )

    with mail.connect(cfg.imap) as mailbox:
        pending = len(mailbox.unread_uids())
    log.info(
        "IMAP OK: %s@%s:%d, mailbox %s, %d unread message(s) waiting",
        cfg.imap.username,
        cfg.imap.host,
        cfg.imap.port,
        cfg.imap.mailbox,
        pending,
    )

    log.info("check passed — on_success is set to %r", cfg.imap.on_success)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # Bootstrap logging so config errors are reported in a sane format.
    _configure_logging(args.log_level or "INFO", "text")

    try:
        cfg = _load_config(args.config)
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return EXIT_CONFIG_ERROR

    _configure_logging(args.log_level or cfg.logging.level, cfg.logging.format)

    lock_path = Path(cfg.lock_file) if cfg.lock_file else None

    try:
        with locking.single_instance(lock_path):
            if args.check:
                return _run_check(cfg)

            summary = runner.run_once(cfg, dry_run=args.dry_run)

            log.info("%s: %s", "dry run complete (counts are hypothetical)" if args.dry_run else "run complete", summary.line())
            if not args.dry_run:
                runner.notify_result(cfg, summary)

            return EXIT_OK if summary.ok else EXIT_PARTIAL_FAILURE

    except locking.AlreadyRunning as exc:
        log.warning("previous run still in progress (lock held on %s); exiting without doing anything", exc)
        return EXIT_OK

    except PapraAuthError as exc:
        log.error("%s", exc)
        runner.notify_failure(cfg, str(exc))
        return EXIT_CONFIG_ERROR

    except mail.MailboxError as exc:
        log.error("IMAP failure: %s", exc)
        runner.notify_failure(cfg, f"IMAP failure: {exc}")
        return EXIT_CONNECTION_ERROR

    except PapraError as exc:
        log.error("Papra failure: %s", exc)
        runner.notify_failure(cfg, f"Papra failure: {exc}")
        return EXIT_CONNECTION_ERROR

    except KeyboardInterrupt:  # pragma: no cover
        log.warning("interrupted; unprocessed mail is untouched and will be retried")
        return EXIT_CONNECTION_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
