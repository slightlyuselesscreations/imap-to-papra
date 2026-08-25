"""Papra REST client: preflight, upload and verification.

The upload contract this relies on (docs.papra.app / papra-hq/papra):

    POST {base}/api/organizations/{orgId}/documents   multipart, file field "file"
      2xx -> {"document": {"id": ...}}
      409 -> code "document.already_exists" (Papra deduplicates on SHA-256)
      413 -> code "document.size_too_large"

    GET  {base}/api/organizations/{orgId}/custom-properties  -> {"propertyDefinitions": [...]}
    POST {base}/api/organizations/{orgId}/custom-properties     json {"name", "type"}
    PUT  {base}/api/organizations/{orgId}/documents/{docId}/custom-properties/{defId}
                                                               json {"value": ...} -> 204

The 409 is what makes this whole tool safe to re-run. If we upload a document
and then crash before deleting the mail, the next pass re-uploads, gets a 409,
and treats it as already archived. At-least-once delivery, exactly-once storage,
with no local state to keep in sync.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

from imap_to_papra.attachments import Attachment
from imap_to_papra.config import PapraConfig

log = logging.getLogger(__name__)

REQUIRED_PERMISSIONS = ("documents:create", "documents:read")
# Only needed for the custom properties, which are best-effort, so a key
# without them still archives mail. Preflight warns rather than aborting.
PROPERTY_PERMISSIONS = ("custom-properties:read", "custom-properties:create", "documents:update")
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class PapraError(Exception):
    """Base class for Papra API failures."""


class PapraAuthError(PapraError):
    """Credentials or permissions are wrong — the whole run should abort."""


class PapraUploadError(PapraError):
    """This attachment could not be stored; the message must not be deleted."""


@dataclass(frozen=True)
class UploadResult:
    filename: str
    document_id: str | None
    deduplicated: bool

    @property
    def summary(self) -> str:
        if self.deduplicated:
            return f"{self.filename}: already in Papra (deduplicated)"
        return f"{self.filename}: uploaded as {self.document_id}"


def _error_code(response: requests.Response) -> str:
    """Best-effort extraction of Papra's error code from a response body."""
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    for candidate in (body.get("error"), body):
        if isinstance(candidate, dict):
            code = candidate.get("code")
            if isinstance(code, str):
                return code
    return ""


class PapraClient:
    """Thin synchronous client scoped to a single organization."""

    def __init__(self, cfg: PapraConfig) -> None:
        self._cfg = cfg
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {cfg.api_key}",
                "Accept": "application/json",
            }
        )
        self._session.verify = cfg.verify_ssl

        if not cfg.verify_ssl:
            log.warning("Papra certificate verification is disabled (papra.verify_ssl = false)")
        if cfg.base_url.startswith("http://"):
            log.warning("papra.base_url uses http://, so the API key is sent unencrypted")

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "PapraClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- helpers

    def _url(self, path: str) -> str:
        return f"{self._cfg.base_url}{path}"

    def _org_url(self, suffix: str = "") -> str:
        return self._url(f"/api/organizations/{self._cfg.organization_id}/documents{suffix}")

    def _properties_url(self, suffix: str = "") -> str:
        return self._url(f"/api/organizations/{self._cfg.organization_id}/custom-properties{suffix}")

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Issue a request, retrying transient failures with exponential backoff.

        Only network errors and 5xx/429 are retried. A 4xx is a decision by the
        server and repeating it would just be noise.
        """
        last_error: Exception | None = None

        for attempt in range(1, self._cfg.max_retries + 1):
            try:
                response = self._session.request(
                    method, url, timeout=self._cfg.timeout_seconds, **kwargs
                )
            except requests.RequestException as exc:
                last_error = exc
                log.warning("papra request failed (attempt %d/%d): %s", attempt, self._cfg.max_retries, exc)
            else:
                if response.status_code not in _RETRYABLE_STATUSES:
                    return response
                last_error = PapraError(f"HTTP {response.status_code} from {url}")
                log.warning(
                    "papra returned %d (attempt %d/%d)",
                    response.status_code,
                    attempt,
                    self._cfg.max_retries,
                )

            if attempt < self._cfg.max_retries:
                time.sleep(2 ** (attempt - 1))

        raise PapraError(f"{method} {url} failed after {self._cfg.max_retries} attempts: {last_error}")

    # -------------------------------------------------------------- preflight

    def preflight(self) -> dict[str, Any]:
        """Verify the base URL, API key and permissions before touching any mail.

        Failing here costs one request; failing halfway through a batch leaves
        mail in a half-processed state, so it is worth checking up front.
        """
        response = self._request("GET", self._url("/api/api-keys/current"))

        if response.status_code in (401, 403):
            raise PapraAuthError(
                f"Papra rejected the API key (HTTP {response.status_code}). "
                "Check papra.api_key and that the key has not expired."
            )
        if response.status_code == 404:
            raise PapraError(
                f"{self._cfg.base_url} responded 404 for /api/api-keys/current — "
                "papra.base_url should point at the Papra root, without a trailing /api."
            )
        if not response.ok:
            raise PapraError(f"Papra preflight failed: HTTP {response.status_code}")

        try:
            body = response.json()
        except ValueError as exc:
            raise PapraError(
                f"{self._cfg.base_url} did not return JSON for /api/api-keys/current — "
                "is papra.base_url pointing at Papra and not at a reverse-proxy error page?"
            ) from exc

        if not isinstance(body, dict):
            raise PapraError("Papra returned an unexpected shape for /api/api-keys/current")

        # Papra wraps the key under "apiKey"; accept a bare object too, since
        # the envelope is not a documented contract.
        info = body.get("apiKey") if isinstance(body.get("apiKey"), dict) else body

        permissions = info.get("permissions")
        if isinstance(permissions, list):
            missing = [name for name in REQUIRED_PERMISSIONS if name not in permissions]
            if missing:
                raise PapraAuthError(
                    f"Papra API key is missing required permission(s): {', '.join(missing)}. "
                    f"Grant them in the Papra dashboard; the key currently has: {', '.join(permissions) or 'none'}."
                )

            lacking = [name for name in PROPERTY_PERMISSIONS if name not in permissions]
            if lacking:
                log.warning(
                    "Papra API key is missing %s, so documents will be archived but not "
                    "labelled with the email subject, sender and date. Grant them in the "
                    "Papra dashboard to enable it.",
                    ", ".join(lacking),
                )

        return info

    # ----------------------------------------------------------------- upload

    def upload(self, attachment: Attachment) -> UploadResult:
        """Store one attachment, treating a duplicate as success."""
        files = {"file": (attachment.filename, attachment.payload, attachment.content_type)}
        data = [("ocrLanguages", lang) for lang in self._cfg.ocr_languages]

        response = self._request("POST", self._org_url(), files=files, data=data or None)

        if response.status_code == 409:
            code = _error_code(response)
            if code and code != "document.already_exists":
                raise PapraUploadError(f"{attachment.filename}: Papra returned 409 {code}")
            log.info("%s already present in Papra (deduplicated by content hash)", attachment.filename)
            return UploadResult(attachment.filename, document_id=None, deduplicated=True)

        if response.status_code in (401, 403):
            raise PapraAuthError(
                f"Papra rejected the API key while uploading {attachment.filename} "
                f"(HTTP {response.status_code}) — check the documents:create permission."
            )

        if response.status_code == 413:
            raise PapraUploadError(
                f"{attachment.filename}: Papra rejected {attachment.size} bytes as too large. "
                "Raise DOCUMENT_STORAGE_MAX_UPLOAD_SIZE on the server, or lower "
                "attachments.max_size_bytes so it is filtered before upload."
            )

        if not response.ok:
            raise PapraUploadError(
                f"{attachment.filename}: upload failed with HTTP {response.status_code} "
                f"{_error_code(response) or response.text[:200]}"
            )

        document_id = self._document_id(response)
        if not document_id:
            raise PapraUploadError(
                f"{attachment.filename}: Papra accepted the upload but returned no document id"
            )

        return UploadResult(attachment.filename, document_id=document_id, deduplicated=False)

    @staticmethod
    def _document_id(response: requests.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return ""
        if not isinstance(body, dict):
            return ""
        document = body.get("document")
        if isinstance(document, dict) and isinstance(document.get("id"), str):
            return document["id"]
        if isinstance(body.get("id"), str):
            return body["id"]
        return ""

    # ------------------------------------------------------- custom properties

    def property_definitions(self, wanted: dict[str, str]) -> dict[str, str]:
        """Map each wanted property name to its definition id, creating misses.

        Papra does not enforce unique property names, so creating blindly would
        add a second "Email subject" on every run. The organization's existing
        definitions are listed first and matched case-insensitively.

        Definition ids are stable, so this runs once per pass, not per document.
        """
        response = self._request("GET", self._properties_url())
        if response.status_code in (401, 403):
            raise PapraAuthError(
                f"Papra rejected the API key while listing custom properties "
                f"(HTTP {response.status_code}) - the key needs custom-properties:read."
            )
        if not response.ok:
            raise PapraError(f"could not list custom properties: HTTP {response.status_code}")

        existing = {
            definition["name"].casefold(): definition["id"]
            for definition in _definition_list(response)
            if isinstance(definition.get("name"), str) and isinstance(definition.get("id"), str)
        }

        resolved: dict[str, str] = {}
        for name, property_type in wanted.items():
            found = existing.get(name.casefold())
            resolved[name] = found if found else self._create_definition(name, property_type)
        return resolved

    def _create_definition(self, name: str, property_type: str) -> str:
        response = self._request(
            "POST", self._properties_url(), json={"name": name, "type": property_type}
        )
        if response.status_code in (401, 403):
            raise PapraAuthError(
                f"Papra rejected the API key while creating the {name!r} custom property "
                f"(HTTP {response.status_code}) - the key needs custom-properties:create."
            )
        if not response.ok:
            raise PapraError(
                f"could not create the {name!r} custom property "
                f"(HTTP {response.status_code} {_error_code(response) or response.text[:200]})"
            )

        definition_id = _definition_id(response)
        if not definition_id:
            raise PapraError(f"Papra created the {name!r} custom property but returned no id")

        log.info("created custom property %r (%s) in Papra", name, property_type)
        return definition_id

    def set_property(self, document_id: str, definition_id: str, value: object) -> None:
        """Set one custom property value on a document. Papra answers 204."""
        response = self._request(
            "PUT",
            self._org_url(f"/{document_id}/custom-properties/{definition_id}"),
            json={"value": value},
        )
        if response.status_code in (401, 403):
            raise PapraAuthError(
                f"Papra rejected the API key while setting a custom property "
                f"(HTTP {response.status_code}) - the key needs documents:update."
            )
        if not response.ok:
            raise PapraError(
                f"could not set custom property {definition_id} on document {document_id} "
                f"(HTTP {response.status_code} {_error_code(response) or response.text[:200]})"
            )

    # ----------------------------------------------------------- verification

    def verify(self, document_id: str, attachment: Attachment) -> None:
        """Read the document back and confirm it is really stored.

        When Papra exposes the stored SHA-256 we compare it against the bytes we
        sent, which is a genuine content-level guarantee rather than just
        "the id resolves".
        """
        response = self._request("GET", self._org_url(f"/{document_id}"))

        if response.status_code == 404:
            raise PapraUploadError(
                f"{attachment.filename}: Papra reported document {document_id} as created, "
                "but reading it back returned 404"
            )
        if not response.ok:
            raise PapraUploadError(
                f"{attachment.filename}: could not verify document {document_id} "
                f"(HTTP {response.status_code})"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise PapraUploadError(f"{attachment.filename}: verification response was not JSON") from exc

        document = body.get("document") if isinstance(body, dict) else None
        if not isinstance(document, dict):
            document = body if isinstance(body, dict) else {}

        stored_hash = _stored_sha256(document)
        if stored_hash and stored_hash.lower() != attachment.sha256:
            raise PapraUploadError(
                f"{attachment.filename}: content mismatch — Papra stored {stored_hash[:12]}… "
                f"but the attachment hashes to {attachment.sha256[:12]}…"
            )

        log.debug(
            "verified %s as document %s%s",
            attachment.filename,
            document_id,
            " (sha256 matched)" if stored_hash else "",
        )


def _definition_list(response: requests.Response) -> list[dict[str, Any]]:
    """The definitions out of a list response, whatever envelope Papra uses."""
    try:
        body = response.json()
    except ValueError:
        return []
    if isinstance(body, list):
        candidates = body
    elif isinstance(body, dict):
        candidates = body.get("propertyDefinitions") or body.get("customProperties") or []
    else:
        return []
    return [item for item in candidates if isinstance(item, dict)]


def _definition_id(response: requests.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    for candidate in (body.get("propertyDefinition"), body.get("customProperty"), body):
        if isinstance(candidate, dict) and isinstance(candidate.get("id"), str):
            return candidate["id"]
    return ""


def _stored_sha256(document: dict[str, Any]) -> str:
    """Pull the stored content hash out of a document, whatever it is called."""
    for key in ("originalSha256Hash", "original_sha256_hash", "sha256Hash", "sha256"):
        value = document.get(key)
        if isinstance(value, str) and len(value) == 64:
            return value
    return ""
