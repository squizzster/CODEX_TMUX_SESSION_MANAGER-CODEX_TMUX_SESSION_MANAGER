"""Authoritative process, handshake, and method contract for Codex App Server."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .version import RODEX_VERSION


class RodexAppServerCompatibilityError(RuntimeError):
    """The live App Server is outside Rodex's exact-control contract."""


@dataclass(frozen=True, slots=True)
class AppServerClientInfo:
    name: str
    title: str
    version: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "title": self.title, "version": self.version}


@dataclass(frozen=True, slots=True)
class CodexAppServerContract:
    supported_version: str
    rpc_connection_path: str = "/rpc"
    initialize_method: str = "initialize"
    initialized_method: str = "initialized"
    thread_loaded_list_method: str = "thread/loaded/list"
    thread_read_method: str = "thread/read"
    thread_status_changed_method: str = "thread/status/changed"
    turn_start_method: str = "turn/start"
    turn_started_method: str = "turn/started"
    turn_steer_method: str = "turn/steer"
    turn_interrupt_method: str = "turn/interrupt"
    turn_completed_method: str = "turn/completed"

    def command(self, codex_binary: str, socket_path: Path) -> tuple[str, ...]:
        return (
            codex_binary,
            "app-server",
            "--listen",
            f"unix://{socket_path}",
        )

    def request(
        self,
        request_id: int | str,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        return {"method": method, "id": request_id, "params": params}

    def notification(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"method": method, "params": params}

    def initialize_params(
        self,
        client: AppServerClientInfo,
        *,
        experimental_api: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"clientInfo": client.as_dict()}
        if experimental_api:
            params["capabilities"] = {"experimentalApi": True}
        return params

    def initialize_request(
        self,
        request_id: int | str,
        client: AppServerClientInfo,
        *,
        experimental_api: bool = False,
    ) -> dict[str, Any]:
        return self.request(
            request_id,
            self.initialize_method,
            self.initialize_params(client, experimental_api=experimental_api),
        )

    def initialized_notification(self) -> dict[str, Any]:
        return self.notification(self.initialized_method, {})

    def version(self, initialize_result: dict[str, Any]) -> str:
        user_agent = initialize_result.get("userAgent")
        if not isinstance(user_agent, str):
            return "unknown"
        product = user_agent.split(" ", 1)[0]
        _client_name, separator, version = product.rpartition("/")
        return version if separator and version else "unknown"

    def require_supported_version(self, initialize_result: dict[str, Any]) -> str:
        version = self.version(initialize_result)
        if version == "unknown":
            raise RodexAppServerCompatibilityError(
                "App Server initialize response has no recognized Codex user agent"
            )
        if version != self.supported_version:
            raise RodexAppServerCompatibilityError(
                "exact control supports Codex App Server "
                f"{self.supported_version}; live server is {version}"
            )
        return version


CODEX_APP_SERVER: Final = CodexAppServerContract(supported_version="0.147.0")
RODEX_CONTROL_APP_SERVER_CLIENT: Final = AppServerClientInfo(
    name="rodex-control",
    title="Rodex Control",
    version=RODEX_VERSION,
)
RODEX_RUNTIME_APP_SERVER_CLIENT: Final = AppServerClientInfo(
    name="rodex",
    title="Rodex",
    version=RODEX_VERSION,
)
RODEX_SESSION_CATALOG_APP_SERVER_CLIENT: Final = AppServerClientInfo(
    name="rodex-session-catalog",
    title="Rodex Session Catalog",
    version=RODEX_VERSION,
)
