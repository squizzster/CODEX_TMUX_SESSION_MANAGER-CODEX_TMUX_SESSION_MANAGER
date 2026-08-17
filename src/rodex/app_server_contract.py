"""Narrow compatibility gate for Rodex's exact App Server control surface."""

from __future__ import annotations

from typing import Any, Final

SUPPORTED_CODEX_APP_SERVER_VERSION: Final = "0.147.0"


class RodexAppServerCompatibilityError(RuntimeError):
    """The live App Server is outside Rodex's characterized exact-control contract."""


def require_supported_app_server(initialize_result: dict[str, Any]) -> str:
    """Return the live version or fail closed for exact machine mutations."""
    user_agent = initialize_result.get("userAgent")
    if not isinstance(user_agent, str):
        raise RodexAppServerCompatibilityError(
            "App Server initialize response has no recognized Codex user agent"
        )
    product = user_agent.split(" ", 1)[0]
    _client_name, separator, version = product.rpartition("/")
    if not separator or not version:
        raise RodexAppServerCompatibilityError(
            "App Server initialize response has no recognized Codex user agent"
        )
    if version != SUPPORTED_CODEX_APP_SERVER_VERSION:
        raise RodexAppServerCompatibilityError(
            "exact control supports Codex App Server "
            f"{SUPPORTED_CODEX_APP_SERVER_VERSION}; live server is {version}"
        )
    return version
