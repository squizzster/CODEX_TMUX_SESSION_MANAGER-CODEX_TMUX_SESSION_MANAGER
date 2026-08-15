"""Integer-keyed unique cool-name allocation helpers."""

from .functions import (
    CoolName,
    CoolNameError,
    CoolNameGenerationError,
    allocate_unique_cool_name,
    get_unique_id_from_cool_name,
    get_unique_new_cool_name,
    initialise_cool_names_database,
)

__all__ = [
    "CoolName",
    "CoolNameError",
    "CoolNameGenerationError",
    "allocate_unique_cool_name",
    "get_unique_id_from_cool_name",
    "get_unique_new_cool_name",
    "initialise_cool_names_database",
]
