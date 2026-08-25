"""Load and validate the configuration from environment variables.

An .env file is parsed when one is present. Real environment variables take
precedence over it.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

log = logging.getLogger(__name__)

ENV_FILE_VAR = "IMAP_TO_PAPRA_ENV"

DEFAULT_ENV_PATHS = (
    Path(".env"),
    Path("/etc/imap-to-papra/.env"),
)

ON_SUCCESS_CHOICES = ("delete", "move", "mark_read")
LOG_FORMAT_CHOICES = ("text", "json")
NOTIFY_ON_CHOICES = ("success", "error")

# Prefixes owned by this tool. Unrecognised names under them are reported.
OWNED_PREFIXES = ("IMAP_", "PAPRA_", "ATTACHMENTS_", "NTFY_", "LOG_", "LOCK_")

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


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

class _Env:
    """Environment mapping that records which names were read.

    Names under OWNED_PREFIXES that were never read are reported as unknown.
    """

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = values
        self.seen: set[str] = {ENV_FILE_VAR}

    def get(self, name: str) -> str | None:
        self.seen.add(name)
        return self._values.get(name)

    def unknown(self) -> list[str]:
        return sorted(
            name
            for name in self._values
            if name.startswith(OWNED_PREFIXES) and name not in self.seen
        )


def _str(env: _Env, name: str, default: str = "") -> str:
    value = env.get(name)
    return default if value is None else value.strip()


def _bool(env: _Env, name: str, default: bool) -> bool:
    value = env.get(name)
    if value is None or not value.strip():
        return default

    folded = value.strip().lower()
    if folded in _TRUE:
        return True
    if folded in _FALSE:
        return False
    raise ConfigError(
        f"{name} must be one of {', '.join(sorted(_TRUE | _FALSE))}; got {value.strip()!r}"
    )


def _int(env: _Env, name: str, default: int, minimum: int = 0) -> int:
    value = env.get(name)
    if value is None or not value.strip():
        return default

    try:
        parsed = int(value.strip())
    except ValueError:
        raise ConfigError(f"{name} must be a whole number; got {value.strip()!r}") from None
    if parsed < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {parsed}")
    return parsed


def _list(env: _Env, name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """A comma-separated list. Unset means the default; set but empty means none."""
    value = env.get(name)
    if value is None:
        return default
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _extensions(env: _Env, name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """Normalise an extension list so `.PDF`, `PDF` and `pdf` all behave the same."""
    return tuple(item.lower().lstrip(".") for item in _list(env, name, default))


# --------------------------------------------------------------------------- #
# Section builders
# --------------------------------------------------------------------------- #

def _build_imap(env: _Env) -> ImapConfig:
    host = _str(env, "IMAP_HOST")
    if not host:
        raise ConfigError("IMAP_HOST is required")

    username = _str(env, "IMAP_USERNAME")
    if not username:
        raise ConfigError("IMAP_USERNAME is required")

    password = _str(env, "IMAP_PASSWORD")
    if not password:
        raise ConfigError("IMAP_PASSWORD is required")

    use_ssl = _bool(env, "IMAP_SSL", True)
    starttls = _bool(env, "IMAP_STARTTLS", False)
    if use_ssl and starttls:
        raise ConfigError(
            "IMAP_SSL and IMAP_STARTTLS are mutually exclusive: implicit TLS already encrypts the connection"
        )
    if not use_ssl and not starttls:
        raise ConfigError(
            "IMAP_SSL and IMAP_STARTTLS are both false, which would send your password "
            "over an unencrypted connection. Use IMAP_SSL=true (port 993), "
            "or IMAP_STARTTLS=true (port 143)."
        )

    on_success = _str(env, "IMAP_ON_SUCCESS", "delete").lower()
    if on_success not in ON_SUCCESS_CHOICES:
        raise ConfigError(
            f"IMAP_ON_SUCCESS must be one of {', '.join(ON_SUCCESS_CHOICES)}, got {on_success!r}"
        )

    move_to = _str(env, "IMAP_MOVE_TO", "Processed")
    if on_success == "move" and not move_to:
        raise ConfigError('IMAP_MOVE_TO is required when IMAP_ON_SUCCESS="move"')

    return ImapConfig(
        host=host,
        username=username,
        password=password,
        port=_int(env, "IMAP_PORT", 993 if use_ssl else 143, minimum=1),
        mailbox=_str(env, "IMAP_MAILBOX", "INBOX") or "INBOX",
        ssl=use_ssl,
        starttls=starttls,
        verify_ssl=_bool(env, "IMAP_VERIFY_SSL", True),
        on_success=on_success,
        move_to=move_to,
        batch_size=_int(env, "IMAP_BATCH_SIZE", 50),
        timeout_seconds=_int(env, "IMAP_TIMEOUT_SECONDS", 60, minimum=1),
    )


def _build_papra(env: _Env) -> PapraConfig:
    base_url = _str(env, "PAPRA_BASE_URL").rstrip("/")
    if not base_url:
        raise ConfigError("PAPRA_BASE_URL is required")
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError(f"PAPRA_BASE_URL must start with http:// or https://, got {base_url!r}")

    api_key = _str(env, "PAPRA_API_KEY")
    if not api_key:
        raise ConfigError("PAPRA_API_KEY is required")

    organization_id = _str(env, "PAPRA_ORGANIZATION_ID")
    if not organization_id:
        raise ConfigError("PAPRA_ORGANIZATION_ID is required")

    return PapraConfig(
        base_url=base_url,
        api_key=api_key,
        organization_id=organization_id,
        verify_ssl=_bool(env, "PAPRA_VERIFY_SSL", True),
        timeout_seconds=_int(env, "PAPRA_TIMEOUT_SECONDS", 60, minimum=1),
        ocr_languages=_list(env, "PAPRA_OCR_LANGUAGES", ()),
        max_retries=_int(env, "PAPRA_MAX_RETRIES", 3, minimum=1),
    )


def _build_attachments(env: _Env) -> AttachmentsConfig:
    defaults = AttachmentsConfig()

    min_size = _int(env, "ATTACHMENTS_MIN_SIZE_BYTES", defaults.min_size_bytes)
    max_size = _int(env, "ATTACHMENTS_MAX_SIZE_BYTES", defaults.max_size_bytes, minimum=1)
    if min_size >= max_size:
        raise ConfigError(
            f"ATTACHMENTS_MIN_SIZE_BYTES ({min_size}) must be smaller than "
            f"ATTACHMENTS_MAX_SIZE_BYTES ({max_size})"
        )

    allowed = _extensions(env, "ATTACHMENTS_ALLOWED", defaults.allowed)
    denied = _extensions(env, "ATTACHMENTS_DENIED", defaults.denied)
    overlap = sorted(set(allowed) & set(denied))
    if overlap:
        raise ConfigError(
            f"ATTACHMENTS_ALLOWED and ATTACHMENTS_DENIED both list: {', '.join(overlap)} — "
            "an extension cannot be simultaneously required and forbidden"
        )

    return AttachmentsConfig(
        allowed=allowed,
        denied=denied,
        min_size_bytes=min_size,
        max_size_bytes=max_size,
        skip_inline=_bool(env, "ATTACHMENTS_SKIP_INLINE", defaults.skip_inline),
    )


def _build_ntfy(env: _Env) -> NtfyConfig:
    defaults = NtfyConfig()

    enabled = _bool(env, "NTFY_ENABLED", defaults.enabled)
    server = _str(env, "NTFY_SERVER", defaults.server).rstrip("/")
    topic = _str(env, "NTFY_TOPIC", defaults.topic)

    if enabled:
        if not server.startswith(("http://", "https://")):
            raise ConfigError(f"NTFY_SERVER must start with http:// or https://, got {server!r}")
        if not topic:
            raise ConfigError("NTFY_TOPIC is required when NTFY_ENABLED=true")

    notify_on = tuple(item.lower() for item in _list(env, "NTFY_NOTIFY_ON", defaults.notify_on))
    unknown = sorted(set(notify_on) - set(NOTIFY_ON_CHOICES))
    if unknown:
        raise ConfigError(
            f"NTFY_NOTIFY_ON may only contain {', '.join(NOTIFY_ON_CHOICES)}; got {', '.join(unknown)}"
        )

    return NtfyConfig(
        enabled=enabled,
        server=server,
        topic=topic,
        token=_str(env, "NTFY_TOKEN"),
        priority=_str(env, "NTFY_PRIORITY", defaults.priority) or defaults.priority,
        notify_on=notify_on,
        timeout_seconds=_int(env, "NTFY_TIMEOUT_SECONDS", defaults.timeout_seconds, minimum=1),
    )


def _build_logging(env: _Env) -> LoggingConfig:
    defaults = LoggingConfig()

    level = _str(env, "LOG_LEVEL", defaults.level).upper() or defaults.level
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise ConfigError(f"LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL; got {level!r}")

    fmt = _str(env, "LOG_FORMAT", defaults.format).lower() or defaults.format
    if fmt not in LOG_FORMAT_CHOICES:
        raise ConfigError(f"LOG_FORMAT must be one of {', '.join(LOG_FORMAT_CHOICES)}; got {fmt!r}")

    return LoggingConfig(level=level, format=fmt)


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #

def from_env(values: Mapping[str, str]) -> Config:
    """Build a validated Config from a mapping of environment variables."""
    env = _Env(values)

    config = Config(
        imap=_build_imap(env),
        papra=_build_papra(env),
        attachments=_build_attachments(env),
        ntfy=_build_ntfy(env),
        logging=_build_logging(env),
        lock_file=_str(env, "LOCK_FILE"),
    )

    unknown = env.unknown()
    if unknown:
        log.warning(
            "ignoring unrecognised setting(s): %s — check the spelling against .env.example",
            ", ".join(unknown),
        )

    return config


def parse_env_file(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines from an .env file.

    Blank lines, # comments, a leading `export` and surrounding single or double
    quotes are handled. A # inside a value is kept.
    """
    values: dict[str, str] = {}

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export "):].lstrip()

        name, separator, value = stripped.partition("=")
        if not separator:
            continue

        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]

        if name:
            values[name] = value

    return values


def default_env_file() -> Path | None:
    """First existing path among $IMAP_TO_PAPRA_ENV, ./.env and /etc/imap-to-papra/.env."""
    from_env_var = os.environ.get(ENV_FILE_VAR)
    if from_env_var:
        return Path(from_env_var)
    for candidate in DEFAULT_ENV_PATHS:
        if candidate.is_file():
            return candidate
    return None


def load(env_file: Path | None = None) -> Config:
    """Read the environment, layered over an .env file when one is given.

    Real environment variables take precedence over the file.
    """
    values: dict[str, str] = {}

    if env_file is not None:
        try:
            values.update(parse_env_file(env_file.read_text(encoding="utf-8")))
        except FileNotFoundError as exc:
            raise ConfigError(f"env file not found: {env_file}") from exc
        except OSError as exc:
            raise ConfigError(f"cannot read env file {env_file}: {exc.strerror}") from exc
        log.debug("loaded %d setting(s) from %s", len(values), env_file)

    values.update(os.environ)
    return from_env(values)
