"""Exceptions specific to Phase 1 (parsing, matching, duplicate detection)."""


class ParseError(Exception):
    """A file could not even be classified/sniffed — it doesn't match the
    expected shape for its format at all."""


class ParseResidueError(Exception):
    """One or more lines/blocks inside an otherwise-recognized file could
    not be turned into a canonical line. Raised with every residue record
    already written to disk (never raised silently) so a human can see
    exactly what was unparseable and why."""


class LineConservationError(Exception):
    """The line-conservation invariant was violated: a line appeared,
    vanished, or was double-counted somewhere between parsing and
    matching."""
