"""Small transactional SQLite contracts shared by Rodex domains."""

from .database import (
    RODEX_DATABASE_FILENAME,
    RODEX_DATABASE_SCHEMA_GENERATION,
    RodexDatabaseNotFoundError,
    RodexSQLError,
    default_rodex_database_path,
    normalise_rodex_database_path,
    open_rodex_read_transaction,
    open_rodex_transaction,
    require_existing_rodex_database_path,
    select_lookup_id,
    select_or_insert_lookup_id,
)
from .index_retry import INDEX_RE_TRY_ATTEMPTS, index_re_try_attempt_numbers

__all__ = [
    "INDEX_RE_TRY_ATTEMPTS",
    "RODEX_DATABASE_FILENAME",
    "RODEX_DATABASE_SCHEMA_GENERATION",
    "RodexDatabaseNotFoundError",
    "RodexSQLError",
    "default_rodex_database_path",
    "index_re_try_attempt_numbers",
    "normalise_rodex_database_path",
    "open_rodex_read_transaction",
    "open_rodex_transaction",
    "require_existing_rodex_database_path",
    "select_lookup_id",
    "select_or_insert_lookup_id",
]
