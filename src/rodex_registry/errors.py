"""Registry-domain failures shared by lifecycle and statistics pipelines."""


class RodexSessionError(RuntimeError):
    """The Rodex session registry could not satisfy its contract."""


class RodexSessionIdCollisionError(RodexSessionError):
    """All permitted 64-bit session ID candidates were occupied."""


class RodexRuntimeIdCollisionError(RodexSessionError):
    """All permitted 64-bit runtime ID candidates were occupied."""


class RodexSessionStatisticsConflictError(RodexSessionError):
    """A statistics publication lost its identity or publication-sequence fence."""


class RodexAnalyticsPublicationRetryableError(RodexSessionError):
    """A transient SQLite lock prevented an analytics publication commit."""


class RodexSessionTurnStatisticsAmbiguousError(RodexSessionError):
    """One unqualified turn ID exists in multiple Codex lineage sources."""
