"""Load and validate the TOML configuration."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ENV_PREFIX = "IMAP_TO_PAPRA"

ON_SUCCESS_CHOICES = ("delete", "move", "mark_read")
LOG_FORMAT_CHOICES = ("text", "json")
NOTIFY_ON_CHOICES = ("success", "error")

DEFAULT_CONFIG_PATHS = (
    Path("config.toml"),
    Path("/etc/imap-to-papra/config.toml"),
)


class ConfigError(Exception):
    """Raised when the configuration is missing, malformed or self-contradictory."""


@dataclass(frozen=True)
class ImapConfig:
    host: str
    username: str
    password: str
    port: int = 993
    mailbox: str = "INBOX"
    ssl: bool = True
    starttls: bool = False
    verify_ssl: bool = True
    on_success: str = "delete"
    move_to: str = "Processed"
    batch_size: int = 50
    timeout_seconds: int = 60


@dataclass(frozen=True)
class PapraConfig:
    base_url: str
    api_key: str
    organization_id: str
    verify_ssl: bool = True
    timeout_seconds: int = 60
    ocr_languages: tuple[str, ...] = ()
    max_retries: int = 3


@dataclass(frozen=True)
class AttachmentsConfig:
    allowed: tuple[str, ...] = ()
    denied: tuple[str, ...] = ("p7s", "asc", "vcf", "ics")
    min_size_bytes: int = 1024
    max_size_bytes: int = 26_214_400
    skip_inline: bool = True


@dataclass(frozen=True)
class NtfyConfig:
    enabled: bool = False
    server: str = "https://ntfy.sh"
    topic: str = ""
    token: str = ""
    priority: str = "default"
    notify_on: tuple[str, ...] = ("success", "error")
    timeout_seconds: int = 10

    def wants(self, event: str) -> bool:
        return self.enabled and event in self.notify_on


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    format: str = "text"


@dataclass(frozen=True)
class Config:
    imap: ImapConfig
    papra: PapraConfig
    attachments: AttachmentsConfig = field(default_factory=AttachmentsConfig)
    ntfy: NtfyConfig = field(default_factory=NtfyConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    lock_file: str = ""


# --------------------------------------------------------------------------- #
# Primitive readers
# --------------------------------------------------------------------------- #

def _table(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table, got {type(value).__name__}")
    return value


def _str(table: dict[str, Any], key: str, section: str, default: str = "") -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{section}.{key} must be a string")
    return value.strip()


def _bool(table: dict[str, Any], key: str, section: str, default: bool) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{section}.{key} must be true or false")
    return value


def _int(table: dict[str, Any], key: str, section: str, default: int, minimum: int = 0) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{section}.{key} must be an integer")
    if value < minimum:
        raise ConfigError(f"{section}.{key} must be >= {minimum}, got {value}")
    return value


def _str_list(table: dict[str, Any], key: str, section: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = table.get(key, list(default))
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{section}.{key} must be an array of strings")
    return tuple(item.strip() for item in value if item.strip())


def _extensions(table: dict[str, Any], key: str, section: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """Normalise an extension list so `.PDF`, `PDF` and `pdf` all behave the same."""
    return tuple(item.lower().lstrip(".") for item in _str_list(table, key, section, default))


# --------------------------------------------------------------------------- #
# Section builders
# --------------------------------------------------------------------------- #

def _build_imap(raw: dict[str, Any]) -> ImapConfig:
    table = _table(raw, "imap")
    section = "imap"

    host = _str(table, "host", section)
    if not host:
        raise ConfigError("imap.host is required")

    username = _str(table, "username", section)
    if not username:
        raise ConfigError("imap.username is required")

    password = _str(table, "password", section)
    if not password:
        raise ConfigError("imap.password is required")

    use_ssl = _bool(table, "ssl", section, True)
    starttls = _bool(table, "starttls", section, False)
    if use_ssl and starttls:
        raise ConfigError("imap.ssl and imap.starttls are mutually exclusive: implicit TLS already encrypts the connection")
    if not use_ssl and not starttls:
        raise ConfigError(
            "imap.ssl and imap.starttls are both false, which would send your password "
            "over an unencrypted connection. Use ssl = true (port 993), "
            "or starttls = true (port 143)."
        )

    on_success = _str(table, "on_success", section, "delete").lower()
    if on_success not in ON_SUCCESS_CHOICES:
        raise ConfigError(f"imap.on_success must be one of {', '.join(ON_SUCCESS_CHOICES)}, got {on_success!r}")

    move_to = _str(table, "move_to", section, "Processed")
    if on_success == "move" and not move_to:
        raise ConfigError("imap.move_to is required when imap.on_success = \"move\"")

    return ImapConfig(
        host=host,
        username=username,
        password=password,
        port=_int(table, "port", section, 993 if use_ssl else 143, minimum=1),
        mailbox=_str(table, "mailbox", section, "INBOX") or "INBOX",
        ssl=use_ssl,
        starttls=starttls,
        verify_ssl=_bool(table, "verify_ssl", section, True),
        on_success=on_success,
        move_to=move_to,
        batch_size=_int(table, "batch_size", section, 50),
        timeout_seconds=_int(table, "timeout_seconds", section, 60, minimum=1),
    )


def _build_papra(raw: dict[str, Any]) -> PapraConfig:
    table = _table(raw, "papra")
    section = "papra"

    base_url = _str(table, "base_url", section).rstrip("/")
    if not base_url:
        raise ConfigError("papra.base_url is required")
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError(f"papra.base_url must start with http:// or https://, got {base_url!r}")

    api_key = _str(table, "api_key", section)
    if not api_key:
        raise ConfigError("papra.api_key is required")

    organization_id = _str(table, "organization_id", section)
    if not organization_id:
        raise ConfigError("papra.organization_id is required")

    return PapraConfig(
        base_url=base_url,
        api_key=api_key,
        organization_id=organization_id,
        verify_ssl=_bool(table, "verify_ssl", section, True),
        timeout_seconds=_int(table, "timeout_seconds", section, 60, minimum=1),
        ocr_languages=_str_list(table, "ocr_languages", section, ()),
        max_retries=_int(table, "max_retries", section, 3, minimum=1),
    )


def _build_attachments(raw: dict[str, Any]) -> AttachmentsConfig:
    table = _table(raw, "attachments")
    section = "attachments"

    defaults = AttachmentsConfig()
    min_size = _int(table, "min_size_bytes", section, defaults.min_size_bytes)
    max_size = _int(table, "max_size_bytes", section, defaults.max_size_bytes, minimum=1)
    if min_size >= max_size:
        raise ConfigError(f"attachments.min_size_bytes ({min_size}) must be smaller than max_size_bytes ({max_size})")

    allowed = _extensions(table, "allowed", section, defaults.allowed)
    denied = _extensions(table, "denied", section, defaults.denied)
    overlap = sorted(set(allowed) & set(denied))
    if overlap:
        raise ConfigError(
            f"attachments.allowed and attachments.denied both list: {', '.join(overlap)} — "
            "an extension cannot be simultaneously required and forbidden"
        )

    return AttachmentsConfig(
        allowed=allowed,
        denied=denied,
        min_size_bytes=min_size,
        max_size_bytes=max_size,
        skip_inline=_bool(table, "skip_inline", section, defaults.skip_inline),
    )


def _build_ntfy(raw: dict[str, Any]) -> NtfyConfig:
    table = _table(raw, "ntfy")
    section = "ntfy"
    defaults = NtfyConfig()

    enabled = _bool(table, "enabled", section, defaults.enabled)
    server = _str(table, "server", section, defaults.server).rstrip("/")
    topic = _str(table, "topic", section, defaults.topic)

    if enabled:
        if not server.startswith(("http://", "https://")):
            raise ConfigError(f"ntfy.server must start with http:// or https://, got {server!r}")
        if not topic:
            raise ConfigError("ntfy.topic is required when ntfy.enabled = true")

    notify_on = tuple(item.lower() for item in _str_list(table, "notify_on", section, defaults.notify_on))
    unknown = sorted(set(notify_on) - set(NOTIFY_ON_CHOICES))
    if unknown:
        raise ConfigError(f"ntfy.notify_on may only contain {', '.join(NOTIFY_ON_CHOICES)}; got {', '.join(unknown)}")

    return NtfyConfig(
        enabled=enabled,
        server=server,
        topic=topic,
        token=_str(table, "token", section),
        priority=_str(table, "priority", section, defaults.priority) or defaults.priority,
        notify_on=notify_on,
        timeout_seconds=_int(table, "timeout_seconds", section, defaults.timeout_seconds, minimum=1),
    )


def _build_logging(raw: dict[str, Any]) -> LoggingConfig:
    table = _table(raw, "logging")
    section = "logging"
    defaults = LoggingConfig()

    level = _str(table, "level", section, defaults.level).upper() or defaults.level
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise ConfigError(f"logging.level must be DEBUG, INFO, WARNING, ERROR or CRITICAL; got {level!r}")

    fmt = _str(table, "format", section, defaults.format).lower() or defaults.format
    if fmt not in LOG_FORMAT_CHOICES:
        raise ConfigError(f"logging.format must be one of {', '.join(LOG_FORMAT_CHOICES)}; got {fmt!r}")

    return LoggingConfig(level=level, format=fmt)


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #

def from_dict(raw: dict[str, Any]) -> Config:
    """Build a validated Config from an already-parsed TOML mapping."""
    known = {"imap", "papra", "attachments", "ntfy", "logging", "lock_file"}
    for unexpected in sorted(set(raw) - known):
        raise ConfigError(f"unknown top-level key {unexpected!r} (expected one of: {', '.join(sorted(known))})")

    lock_file = raw.get("lock_file", "")
    if not isinstance(lock_file, str):
        raise ConfigError("lock_file must be a string path")

    return Config(
        imap=_build_imap(raw),
        papra=_build_papra(raw),
        attachments=_build_attachments(raw),
        ntfy=_build_ntfy(raw),
        logging=_build_logging(raw),
        lock_file=lock_file.strip(),
    )


def default_config_path() -> Path | None:
    """First existing path among $IMAP_TO_PAPRA_CONFIG, ./config.toml and /etc/..."""
    from_env = os.environ.get(f"{ENV_PREFIX}_CONFIG")
    if from_env:
        return Path(from_env)
    for candidate in DEFAULT_CONFIG_PATHS:
        if candidate.is_file():
            return candidate
    return None


def load(path: Path) -> Config:
    """Read and validate the config file at `path`."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc.strerror}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc

    return from_dict(raw)
