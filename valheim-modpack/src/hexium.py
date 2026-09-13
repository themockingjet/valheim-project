"""Hexium public API client."""

from dataclasses import dataclass
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .errors import ModpackError

API_BASE_URL = "https://valheim.hexium.gg/api/experimental/package"
USER_AGENT = "valheim-modpack/0.1"


@dataclass(frozen=True)
class HexiumClient:
    """Minimal client for exactly the public Hexium package endpoints we use."""

    timeout_seconds: int = 30

    def _get_json(self, url: str) -> dict[str, Any]:
        request = Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.load(response)
        except HTTPError as error:
            raise ModpackError(f"Hexium returned HTTP {error.code} for {url}") from error
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            raise ModpackError(f"Cannot retrieve Hexium metadata from {url}: {error}") from error
        if not isinstance(payload, dict):
            raise ModpackError(f"Hexium returned invalid metadata from {url}")
        return payload

    def get_package(self, namespace: str, name: str) -> dict[str, Any]:
        return self._get_json(
            f"{API_BASE_URL}/{quote(namespace, safe='')}/{quote(name, safe='')}/"
        )

    def get_version(self, namespace: str, name: str, version: str) -> dict[str, Any]:
        return self._get_json(
            f"{API_BASE_URL}/{quote(namespace, safe='')}/{quote(name, safe='')}/"
            f"{quote(version, safe='')}/"
        )

    def download(self, url: str) -> bytes:
        request = Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return response.read()
        except (HTTPError, URLError, TimeoutError) as error:
            raise ModpackError(f"Cannot download Hexium archive {url}: {error}") from error
