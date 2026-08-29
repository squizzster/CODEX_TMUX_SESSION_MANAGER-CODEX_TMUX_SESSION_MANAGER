"""Registry-domain failures shared by lifecycle and statistics pipelines."""


class RodexSessionError(RuntimeError):
    """The Rodex session registry could not satisfy its contract."""


class RodexSessionIdCollisionError(RodexSessionError):
    """All permitted 64-bit session ID candidates were occupied."""


class RodexRuntimeIdCollisionError(RodexSessionError):
    """All permitted 64-bit runtime ID candidates were occupied."""


class RodexSessionStatisticsConflictError(RodexSessionError):
    """A statistics publication conflicts with its durable registry state."""


class RodexSessionStatisticsPublicationRaceError(RodexSessionStatisticsConflictError):
    """A publication lost a sequence fence and may succeed after checkpoint reload."""


class RodexAnalyticsPublicationRetryableError(RodexSessionError):
    """A transient SQLite lock prevented an analytics publication commit."""


class RodexSessionTurnStatisticsAmbiguousError(RodexSessionError):
    """One unqualified turn ID exists in multiple Codex lineage sources."""
