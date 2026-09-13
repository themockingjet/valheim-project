"""Small version helpers for Hexium's dotted numeric package versions."""

import re

from .errors import ModpackError

VERSION_PATTERN = re.compile(
    r"^(?P<core>\d+(?:\.\d+)*)(?P<prerelease>-[0-9A-Za-z][0-9A-Za-z.-]*)?$"
)


def parse_version(value: str) -> tuple[tuple[int, ...], str | None]:
    """Parse a Hexium version and reject ambiguous version strings."""

    match = VERSION_PATTERN.fullmatch(value)
    if match is None:
        raise ModpackError(f"Unsupported package version format: {value!r}")
    return tuple(int(part) for part in match.group("core").split(".")), match.group(
        "prerelease"
    )


def is_prerelease(value: str) -> bool:
    """Return whether a valid version contains a prerelease suffix."""

    return parse_version(value)[1] is not None


def at_least(actual: str, minimum: str) -> bool:
    """Compare numeric core versions and treat prereleases as earlier releases."""

    actual_core, actual_prerelease = parse_version(actual)
    minimum_core, minimum_prerelease = parse_version(minimum)
    width = max(len(actual_core), len(minimum_core))
    actual_key = actual_core + (0,) * (width - len(actual_core))
    minimum_key = minimum_core + (0,) * (width - len(minimum_core))

    if actual_key != minimum_key:
        return actual_key > minimum_key
    if actual_prerelease is None:
        return True
    if minimum_prerelease is None:
        return False
    return actual_prerelease >= minimum_prerelease
