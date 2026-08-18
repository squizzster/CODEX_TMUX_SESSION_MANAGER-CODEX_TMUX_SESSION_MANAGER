"""Registry-domain failures shared by lifecycle and statistics pipelines."""


class RodexSessionError(RuntimeError):
    """The Rodex session registry could not satisfy its contract."""


class RodexSessionIdCollisionError(RodexSessionError):
    """All permitted 64-bit session ID candidates were occupied."""


class RodexRuntimeIdCollisionError(RodexSessionError):
    """All permitted 64-bit runtime ID candidates were occupied."""


class RodexSessionStatisticsConflictError(RodexSessionError):
    """A statistics publication lost its identity or revision fence."""


class RodexSessionTurnStatisticsAmbiguousError(RodexSessionError):
    """One unqualified turn ID exists in multiple Codex lineage sources."""
