"""Bounded search over Hexium's fixed public package catalogue."""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

CATALOG_URL = "https://valheim.hexium.gg/api/v1/package/"
MAX_CATALOG_BYTES = 8 * 1024 * 1024
MAX_CATALOG_ITEMS = 5_000
MAX_RESULTS = 10
CATALOG_TTL_SECONDS = 15 * 60
QUERY_PATTERN = re.compile(r"[A-Za-z0-9 _.-]{2,64}\Z")
IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
VERSION_PATTERN = re.compile(r"(?:latest|[A-Za-z0-9][A-Za-z0-9.+-]{0,63})\Z")


class CatalogError(Exception):
    """Hexium catalogue data cannot be safely searched."""


class InvalidSearch(CatalogError):
    """A caller-provided search query is outside the fixed safe grammar."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def _fetch_catalog() -> object:
    request = Request(
        CATALOG_URL,
        headers={"Accept": "application/json", "User-Agent": "valheim-dashboard/0.1"},
    )
    try:
        with build_opener(_NoRedirect()).open(request, timeout=5) as response:
            if response.url != CATALOG_URL:
                raise CatalogError("Hexium catalogue redirect was rejected")
            body = response.read(MAX_CATALOG_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise CatalogError("Hexium catalogue is temporarily unavailable") from error
    if len(body) > MAX_CATALOG_BYTES:
        raise CatalogError("Hexium catalogue exceeds the maximum size")
    try:
        return json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise CatalogError("Hexium catalogue is invalid") from error


def _latest_version(raw_versions: object) -> str | None:
    if not isinstance(raw_versions, list):
        return None
    for version in raw_versions:
        if not isinstance(version, dict) or version.get("is_active") is not True:
            continue
        value = version.get("version_number")
        if isinstance(value, str) and VERSION_PATTERN.fullmatch(value) is not None:
            return value
    return None


def _catalog_entries(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_CATALOG_ITEMS:
        raise CatalogError("Hexium catalogue has an unsupported shape")
    entries: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict) or item.get("is_deprecated") is True:
            continue
        namespace = item.get("owner")
        name = item.get("name")
        version = _latest_version(item.get("versions"))
        if not all(
            isinstance(part, str) and IDENTIFIER_PATTERN.fullmatch(part) is not None
            for part in (namespace, name)
        ) or version is None:
            continue
        latest = next(
            (
                candidate
                for candidate in item["versions"]
                if isinstance(candidate, dict)
                and candidate.get("is_active") is True
                and candidate.get("version_number") == version
            ),
            {},
        )
        description = latest.get("description") if isinstance(latest, dict) else ""
        dependencies = latest.get("dependencies") if isinstance(latest, dict) else []
        entries.append(
            {
                "namespace": namespace,
                "name": name,
                "latest_version": version,
                "description": description[:240] if isinstance(description, str) else "",
                "dependency_count": len(dependencies) if isinstance(dependencies, list) else 0,
            }
        )
    return entries


class HexiumCatalog:
    """Small locked in-memory cache for a fixed, read-only package catalogue."""

    def __init__(self, *, ttl_seconds: float = CATALOG_TTL_SECONDS) -> None:
        self._ttl_seconds = ttl_seconds
        self._entries: list[dict[str, Any]] = []
        self._loaded_at = 0.0
        self._lock = threading.Lock()

    def _get_entries(self) -> list[dict[str, Any]]:
        with self._lock:
            if self._entries and time.monotonic() - self._loaded_at < self._ttl_seconds:
                return self._entries
            self._entries = _catalog_entries(_fetch_catalog())
            self._loaded_at = time.monotonic()
            return self._entries

    def search(self, query: str) -> list[dict[str, Any]]:
        if QUERY_PATTERN.fullmatch(query) is None:
            raise InvalidSearch("Search must be 2-64 letters, numbers, spaces, dots, or dashes")
        normalized = query.casefold().strip()
        matches = [
            entry
            for entry in self._get_entries()
            if normalized in entry["name"].casefold()
            or normalized in entry["namespace"].casefold()
            or normalized in f'{entry["namespace"]}-{entry["name"]}'.casefold()
        ]
        matches.sort(
            key=lambda entry: (
                entry["name"].casefold() != normalized,
                entry["namespace"].casefold() != normalized,
                f'{entry["namespace"]}-{entry["name"]}'.casefold() != normalized,
                entry["name"].casefold(),
                entry["namespace"].casefold(),
            )
        )
        return matches[:MAX_RESULTS]
