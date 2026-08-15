"""Integer-keyed unique cool-name allocation helpers."""

from .functions import (
    RODEX_RESERVED_WORDS,
    CoolName,
    CoolNameError,
    CoolNameGenerationError,
    ReservedCoolNameError,
    allocate_unique_cool_name,
    get_unique_id_from_cool_name,
    get_unique_new_cool_name,
    initialise_cool_names_database,
    is_reserved_rodex_name,
    lookup_cool_name,
    reserve_specific_cool_name,
)

__all__ = [
    "RODEX_RESERVED_WORDS",
    "CoolName",
    "CoolNameError",
    "CoolNameGenerationError",
    "ReservedCoolNameError",
    "allocate_unique_cool_name",
    "get_unique_id_from_cool_name",
    "get_unique_new_cool_name",
    "initialise_cool_names_database",
    "is_reserved_rodex_name",
    "lookup_cool_name",
    "reserve_specific_cool_name",
]
