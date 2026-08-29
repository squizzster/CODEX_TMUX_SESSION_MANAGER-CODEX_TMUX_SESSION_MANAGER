"""Errors shared by Rodex SQLite boundary components."""


class RodexSQLError(RuntimeError):
    """A Rodex SQL operation violated its storage or transaction contract."""


class RodexDatabaseNotFoundError(RodexSQLError):
    """A read-only operation required storage that does not yet exist."""


class RodexDatabaseNotInitializedError(RodexSQLError):
    """Existing storage has not been admitted through Rodex initialization."""


class RodexDatabaseMovedError(RodexSQLError):
    """A transaction identity fence detected changed database storage."""

    code = "database_moved"

    def __init__(self, database_path: object, reason: str) -> None:
        super().__init__(
            f"database_moved: database storage changed at {database_path}: {reason}; "
            "please restart Rodex"
        )
