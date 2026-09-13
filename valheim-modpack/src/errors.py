"""Domain-specific errors surfaced to operators without a traceback."""


class ModpackError(Exception):
    """A manifest, Hexium, or package archive validation failure."""
