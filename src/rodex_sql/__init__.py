"""Small transactional SQLite contracts shared by Rodex domains."""

from .database_location_guard import database_terminal_signal
from .errors import (
    RodexDatabaseMovedError,
    RodexDatabaseNotFoundError,
    RodexDatabaseNotInitializedError,
    RodexSQLError,
)
from .index_retry import INDEX_RE_TRY_ATTEMPTS, index_re_try_attempt_numbers
from .lookups import select_lookup_id, select_or_insert_lookup_id
from .private_database_path import (
    RODEX_DATABASE_FILENAME,
    RODEX_DATABASE_SCHEMA_GENERATION,
    default_rodex_database_path,
    normalise_rodex_database_path,
)
from .transactions import (
    open_rodex_bootstrap_transaction,
    open_rodex_maintenance_lock,
    open_rodex_read_transaction,
    open_rodex_transaction,
    require_active_rodex_transaction,
)

__all__ = [
    "INDEX_RE_TRY_ATTEMPTS",
    "RODEX_DATABASE_FILENAME",
    "RODEX_DATABASE_SCHEMA_GENERATION",
    "RodexDatabaseMovedError",
    "RodexDatabaseNotFoundError",
    "RodexDatabaseNotInitializedError",
    "RodexSQLError",
    "database_terminal_signal",
    "default_rodex_database_path",
    "index_re_try_attempt_numbers",
    "normalise_rodex_database_path",
    "open_rodex_bootstrap_transaction",
    "open_rodex_maintenance_lock",
    "open_rodex_read_transaction",
    "open_rodex_transaction",
    "require_active_rodex_transaction",
    "select_lookup_id",
    "select_or_insert_lookup_id",
]
