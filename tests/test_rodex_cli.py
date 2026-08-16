from __future__ import annotations

import json
import os
import sqlite3
import sys
import uuid
from dataclasses import replace
from pathlib import Path
from threading import Event, Lock, Thread, current_thread
from typing import Any

import pytest

from rodex.cli import RodexExecutableNotFoundError, RodexLaunchError, main, run
from rodex.control import LiveRodexControl, PromptDispatch
from rodex.runtime import (
    LiveRodexRuntime,
    LiveTmuxSession,
    RodexCodexSessionNotFoundError,
    RodexRuntimeError,
)
from rodex_functions import (
    RodexSessionError,
    RodexSessionsUserIdentity,
    assign_a_user_defined_cool_name,
    create_a_rodex_session,
    lookup_codex_uuid_from_a_rodex_session_id,
    lookup_rodex_session_id_from_a_cool_name,
    lookup_rodex_session_names,
    lookup_rodex_tmux_session,
    lookup_rodex_uuid_from_an_id,
    open_a_user_defined_cool_name_assignment,
)

CODEX_UUID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")
REPLACEMENT_CODEX_UUID = uuid.UUID(int=CODEX_UUID.int + 1)
DNA = RodexSessionsUserIdentity(1009, 1010, "dna")


class StubLauncher:
    def __init__(self, tmp_path: Path) -> None:
        self.runtime = LiveRodexRuntime(
            tmux_server_socket_path=tmp_path / "tmux.sock",
            tmux_session_name="rodex-example",
            app_server_socket_path=tmp_path / "app.sock",
            app_server_log_path=tmp_path / "app.log",
            protocol_proxy_socket_path=tmp_path / "proxy.sock",
            protocol_event_socket_path=tmp_path / "events.sock",
        )
        self.started: list[tuple[Path, list[str]]] = []
        self.analytics_identities: list[tuple[uuid.UUID | None, Path | None]] = []
        self.renamed: list[tuple[LiveTmuxSession, str]] = []
        self.configured: list[LiveTmuxSession] = []
        self.attached: list[LiveTmuxSession] = []
        self.stopped: list[tuple[LiveTmuxSession, bool]] = []
        self.existing_checks: list[LiveTmuxSession] = []
        self.live = True
        self.observed_codex_uuid = CODEX_UUID
        self.start_error: RodexRuntimeError | None = None
        self.start_errors: list[RodexRuntimeError] = []
        self.control = LiveRodexControl(
            tmp_path / "proxy.sock", tmp_path / "events.sock", CODEX_UUID
        )
        self.control_discoveries: list[LiveTmuxSession] = []

    def start(
        self,
        workspace: Path,
        arguments: list[str],
        *,
        rodex_session_uuid: uuid.UUID | None = None,
        rodex_database_path: Path | None = None,
    ) -> tuple[LiveRodexRuntime, uuid.UUID]:
        self.started.append((workspace, arguments))
        self.analytics_identities.append((rodex_session_uuid, rodex_database_path))
        if self.start_errors:
            raise self.start_errors.pop(0)
        if self.start_error is not None:
            error = self.start_error
            self.start_error = None
            raise error
        return self.runtime, self.observed_codex_uuid

    def session_exists(self, runtime: LiveTmuxSession) -> bool:
        self.existing_checks.append(runtime)
        return self.live

    def rename(self, runtime: LiveTmuxSession, tmux_session_name: str) -> LiveTmuxSession:
        self.renamed.append((runtime, tmux_session_name))
        return replace(runtime, tmux_session_name=tmux_session_name)

    def configure_identity_status(self, runtime: LiveTmuxSession) -> None:
        self.configured.append(runtime)

    def attach(self, runtime: LiveTmuxSession) -> None:
        self.attached.append(runtime)

    def stop(self, runtime: LiveTmuxSession, *, check: bool = True) -> None:
        self.stopped.append((runtime, check))

    def discover_runtime_control(self, runtime: LiveTmuxSession) -> LiveRodexControl:
        self.control_discoveries.append(runtime)
        return self.control


class StubControlClient:
    def __init__(self) -> None:
        self.sent: list[tuple[LiveRodexControl, str]] = []
        self.waited: list[LiveRodexControl] = []
        self.tailed: list[LiveRodexControl] = []

    def send_prompt(
        self,
        control: LiveRodexControl,
        prompt: str,
        *,
        revalidate: Any,
    ) -> PromptDispatch:
        revalidate()
        self.sent.append((control, prompt))
        return PromptDispatch("started", "turn-1")

    def wait_until_idle(self, control: LiveRodexControl, *, revalidate: Any) -> None:
        revalidate()
        self.waited.append(control)

    def tail(
        self,
        control: LiveRodexControl,
        write_event: Any,
        *,
        revalidate: Any,
    ) -> None:
        revalidate()
        self.tailed.append(control)
        write_event('{"method":"turn/started"}')


class RecordingCodexDelegator:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(self, codex_binary: str, arguments: list[str]) -> int:
        self.calls.append((codex_binary, arguments))
        return self.returncode


def available_prerequisite(command: str) -> str:
    return f"/usr/bin/{command}"


def create_controlled_session(database: Path, tmp_path: Path) -> None:
    create_a_rodex_session(
        database,
        codex_session_uuid=CODEX_UUID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="automatic-beluga",
    )


def test_help_prints_rodex_commands_without_codex_tmux_or_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    delegator = RecordingCodexDelegator()
    monkeypatch.setattr(
        "rodex.cli.shutil.which",
        lambda command: pytest.fail(f"unexpected prerequisite lookup: {command}"),
    )

    assert run(["_help"], database_path=database, codex_delegator=delegator) == 0

    output = capsys.readouterr()
    assert output.err == ""
    assert output.out.startswith("usage: rodex [COMMAND [ARGUMENTS]]\n")
    assert "_help" in output.out
    assert "_create" in output.out
    assert "_running" in output.out
    assert "Every other invocation is passed unchanged to Codex." in output.out
    assert delegator.calls == []
    assert not database.exists()


def test_help_rejects_arguments_without_checking_prerequisites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "rodex.cli.shutil.which",
        lambda command: pytest.fail(f"unexpected prerequisite lookup: {command}"),
    )

    with pytest.raises(RodexLaunchError, match=r"^usage: rodex _help$"):
        run(["_help", "unexpected"], database_path=tmp_path / "rodex.sqlite3")


@pytest.mark.parametrize("command", ["_send"])
def test_send_command_targets_the_verified_named_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_controlled_session(database, tmp_path)
    launcher = StubLauncher(tmp_path)
    control = StubControlClient()

    assert (
        run(
            [command, "automatic-beluga", "run", "tests"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
            control_client=control,  # type: ignore[arg-type]
        )
        == 0
    )

    assert control.sent == [(launcher.control, "run tests")]
    assert launcher.control_discoveries == [
        LiveTmuxSession(tmp_path / "tmux.sock", "automatic-beluga"),
        LiveTmuxSession(tmp_path / "tmux.sock", "automatic-beluga"),
    ]
    assert "started Codex turn turn-1" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["_wait"])
def test_wait_command_waits_for_the_verified_named_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_controlled_session(database, tmp_path)
    launcher = StubLauncher(tmp_path)
    control = StubControlClient()

    assert (
        run(
            [command, "automatic-beluga"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
            control_client=control,  # type: ignore[arg-type]
        )
        == 0
    )

    assert control.waited == [launcher.control]


@pytest.mark.parametrize("command", ["_tail"])
def test_tail_command_streams_json_events_for_the_verified_named_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_controlled_session(database, tmp_path)
    launcher = StubLauncher(tmp_path)
    control = StubControlClient()

    assert (
        run(
            [command, "automatic-beluga"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
            control_client=control,  # type: ignore[arg-type]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert control.tailed == [launcher.control]
    assert captured.out == '{"method":"turn/started"}\n'
    assert "following live Codex protocol events" in captured.err


@pytest.mark.parametrize("arguments", [[], ["_create"]], ids=["bare", "explicit"])
def test_default_and_explicit_create_link_identities_before_attach(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    launcher = StubLauncher(tmp_path)
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug",
        lambda word_count: "automatic-beluga" if word_count == 2 else "unused-name-here",
    )

    assert (
        run(
            arguments,
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )
        == 0
    )

    assert launcher.started == [(Path.cwd(), [])]
    planned_uuid, planned_database = launcher.analytics_identities[0]
    assert planned_uuid == lookup_rodex_uuid_from_an_id(1, database)
    assert planned_database == database
    assert launcher.renamed == [(launcher.runtime, "automatic-beluga")]
    assert launcher.configured[0].tmux_session_name == "automatic-beluga"
    assert launcher.attached == launcher.configured
    tmux_link = lookup_rodex_tmux_session(1, database)
    assert lookup_codex_uuid_from_a_rodex_session_id(1, database) == CODEX_UUID
    assert tmux_link is not None
    assert tmux_link.tmux_server_socket_path == str(
        launcher.runtime.tmux_server_socket_path
    )
    assert tmux_link.tmux_session_name == "automatic-beluga"
    output = capsys.readouterr().out
    assert f"-> Codex {CODEX_UUID}" in output
    assert "Rodex automatic-beluga" in output


@pytest.mark.parametrize(
    "arguments",
    [
        ["--help"],
        ["-h"],
        ["--version"],
        ["-V"],
        ["exec"],
        ["review"],
        ["login"],
        ["logout"],
        ["mcp"],
        ["plugin"],
        ["mcp-server"],
        ["app-server"],
        ["remote-control"],
        ["completion"],
        ["update"],
        ["doctor"],
        ["sandbox"],
        ["debug"],
        ["apply"],
        ["resume"],
        ["archive"],
        ["delete"],
        ["unarchive"],
        ["fork"],
        ["cloud"],
        ["exec-server"],
        ["features"],
        ["help"],
        ["exec", "--json", "run tests"],
        ["review", "--uncommitted"],
        ["features", "list"],
        ["mcp", "list"],
        ["completion", "bash"],
        ["doctor"],
        ["resume", "--last"],
        ["fork", "--last"],
        ["--remote", "unix:///tmp/codex.sock"],
        ["--remote=unix:///tmp/codex.sock"],
        ["--future-codex-option"],
        ["--model", "example"],
        ["future-codex-command", "argument"],
        ["--create", "project_1234"],
        ["--detach"],
        ["--force"],
        ["running"],
    ],
)
def test_non_rodex_invocations_delegate_unchanged_without_tmux_or_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    delegator = RecordingCodexDelegator(returncode=23)
    monkeypatch.setattr(
        "rodex.cli.shutil.which",
        lambda command: "/usr/bin/codex" if command == "codex" else None,
    )

    assert (
        run(arguments, database_path=database, codex_delegator=delegator)
        == delegator.returncode
    )

    assert delegator.calls == [("/usr/bin/codex", arguments)]
    assert not database.exists()


def test_bare_invocation_never_delegates_to_codex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = StubLauncher(tmp_path)
    delegator = RecordingCodexDelegator(returncode=23)
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )

    assert (
        run(
            [],
            database_path=tmp_path / "rodex.sqlite3",
            launcher=launcher,  # type: ignore[arg-type]
            codex_delegator=delegator,
        )
        == 0
    )

    assert delegator.calls == []
    assert launcher.started == [(Path.cwd(), [])]
    assert launcher.attached == launcher.configured


def test_bare_invocation_is_observably_equivalent_to_explicit_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    observed: list[tuple[object, ...]] = []

    for label, arguments in (("bare", []), ("explicit", ["_create"])):
        case_root = tmp_path / label
        launcher = StubLauncher(case_root)
        database = case_root / "rodex.sqlite3"

        assert run(arguments, database_path=database, launcher=launcher) == 0  # type: ignore[arg-type]

        tmux_link = lookup_rodex_tmux_session(1, database)
        names = lookup_rodex_session_names(1, database)
        assert tmux_link is not None
        assert names is not None
        observed.append(
            (
                launcher.started,
                [runtime.tmux_session_name for runtime in launcher.configured],
                [runtime.tmux_session_name for runtime in launcher.attached],
                names.display_name,
                tmux_link.tmux_session_name,
                lookup_codex_uuid_from_a_rodex_session_id(1, database),
            )
        )

    assert observed[0] == observed[1]


def test_create_command_forwards_interactive_codex_arguments_to_managed_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = StubLauncher(tmp_path)
    delegator = RecordingCodexDelegator()
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )

    assert (
        run(
            ["_create", "--model", "review"],
            database_path=tmp_path / "rodex.sqlite3",
            launcher=launcher,  # type: ignore[arg-type]
            codex_delegator=delegator,
        )
        == 0
    )

    assert launcher.started == [(Path.cwd(), ["--model", "review"])]
    assert delegator.calls == []


@pytest.mark.parametrize("create_flag", ["_create"])
def test_explicit_create_assigns_the_requested_display_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    create_flag: str,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    launcher = StubLauncher(tmp_path)
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )

    assert (
        run(
            [create_flag, "project_1234"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )
        == 0
    )

    assert launcher.started == [(Path.cwd(), [])]
    assert launcher.renamed == [(launcher.runtime, "project_1234")]
    assert launcher.attached[0].tmux_session_name == "project_1234"
    names = lookup_rodex_session_names(1, database)
    assert names is not None
    assert names.cool_name == "automatic-beluga"
    assert names.user_defined_cool_name == "project_1234"
    tmux_link = lookup_rodex_tmux_session(1, database)
    assert tmux_link is not None
    assert tmux_link.tmux_session_name == "project_1234"


@pytest.mark.parametrize("create_flag", ["_create"])
def test_explicit_create_forwards_ordinary_codex_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    create_flag: str,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    launcher = StubLauncher(tmp_path)
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )

    assert (
        run(
            [create_flag, "project_1234", "--model", "gpt-5.6-luna"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )
        == 0
    )

    assert launcher.started == [
        (Path.cwd(), ["--model", "gpt-5.6-luna"]),
    ]
    names = lookup_rodex_session_names(1, database)
    assert names is not None
    assert names.display_name == "project_1234"


def test_codex_short_config_passes_through_without_starting_rodex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    delegator = RecordingCodexDelegator()
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)

    assert (
        run(
            ["-c", 'model="gpt-5.6-luna"'],
            database_path=database,
            codex_delegator=delegator,
        )
        == 0
    )

    assert delegator.calls == [("/usr/bin/codex", ["-c", 'model="gpt-5.6-luna"'])]
    assert not database.exists()


@pytest.mark.parametrize(
    "codex_argument",
    ["--create", "--c", "-create", "-d", "--d", "-detach", "--detach"],
)
def test_codex_end_of_options_preserves_rodex_looking_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    codex_argument: str,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    delegator = RecordingCodexDelegator()
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)

    assert (
        run(
            ["exec", "--", codex_argument],
            database_path=database,
            codex_delegator=delegator,
        )
        == 0
    )

    assert delegator.calls == [("/usr/bin/codex", ["exec", "--", codex_argument])]
    assert not database.exists()


def test_rodex_launch_options_are_consumed_only_from_the_command_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delegator = RecordingCodexDelegator()
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    arguments = ["exec", "-d", "--create", "report"]

    assert run(arguments, codex_delegator=delegator) == 0

    assert delegator.calls == [("/usr/bin/codex", arguments)]


def test_explicit_create_can_forward_codex_arguments_after_end_of_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    launcher = StubLauncher(tmp_path)
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )

    assert (
        run(
            ["_create", "project_1234", "--", "--create"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )
        == 0
    )

    assert launcher.started == [(Path.cwd(), ["--create"])]
    names = lookup_rodex_session_names(1, database)
    assert names is not None
    assert names.display_name == "project_1234"


@pytest.mark.parametrize("detach_flag", ["_detach"])
def test_detach_starts_without_attaching_and_prints_compact_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    detach_flag: str,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    launcher = StubLauncher(tmp_path)
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )

    assert (
        run(
            [detach_flag],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )
        == 0
    )

    assert launcher.started == [(Path.cwd(), [])]
    assert launcher.attached == []
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload == {
        "status": "running",
        "rodex_session_name": "automatic-beluga",
        "rodex_session_uuid": str(lookup_rodex_uuid_from_an_id(1, database)),
        "codex_session_uuid": str(CODEX_UUID),
    }
    assert output == f"{json.dumps(payload, indent=2)}\n"


@pytest.mark.parametrize("detach_flag", ["_detach"])
def test_detach_command_forwards_interactive_codex_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    detach_flag: str,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    launcher = StubLauncher(tmp_path)
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )

    assert (
        run(
            [detach_flag, "--model", "gpt-5.6-terra"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )
        == 0
    )

    assert launcher.attached == []
    assert launcher.started == [(Path.cwd(), ["--model", "gpt-5.6-terra"])]
    assert json.loads(capsys.readouterr().out)["rodex_session_name"] == ("automatic-beluga")


@pytest.mark.parametrize("detach_flag", ["_detach"])
def test_detach_existing_name_resolves_without_attaching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    detach_flag: str,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_controlled_session(database, tmp_path)
    launcher = StubLauncher(tmp_path)

    assert (
        run(
            [detach_flag, "automatic-beluga"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )
        == 0
    )

    assert launcher.started == []
    assert launcher.attached == []
    assert json.loads(capsys.readouterr().out)["rodex_session_name"] == ("automatic-beluga")


def test_detach_ended_name_resumes_exact_codex_session_without_attaching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    session = create_a_rodex_session(
        database,
        codex_session_uuid=CODEX_UUID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "stale.sock",
        tmux_session_name="automatic-beluga",
    )
    launcher = StubLauncher(tmp_path)
    launcher.live = False

    assert (
        run(
            ["_detach", "automatic-beluga"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert launcher.started == [(Path.cwd(), ["resume", str(CODEX_UUID)])]
    assert launcher.analytics_identities == [(session.rodex_uuid, database)]
    assert launcher.renamed == [(launcher.runtime, "automatic-beluga")]
    assert launcher.configured[0].tmux_session_name == "automatic-beluga"
    assert launcher.attached == []
    assert output == {
        "status": "running",
        "rodex_session_name": "automatic-beluga",
        "rodex_session_uuid": str(session.rodex_uuid),
        "codex_session_uuid": str(CODEX_UUID),
    }


def test_detach_unsaved_name_recovers_identity_without_attaching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    session = create_a_rodex_session(
        database,
        codex_session_uuid=CODEX_UUID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "stale.sock",
        tmux_session_name="automatic-beluga",
    )
    launcher = StubLauncher(tmp_path)
    launcher.live = False
    launcher.start_error = RodexCodexSessionNotFoundError(
        f"Codex has no saved session for exact identity {CODEX_UUID}"
    )
    launcher.observed_codex_uuid = REPLACEMENT_CODEX_UUID

    assert (
        run(
            ["_detach", "automatic-beluga"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert launcher.started == [
        (Path.cwd(), ["resume", str(CODEX_UUID)]),
        (Path.cwd(), []),
    ]
    assert launcher.analytics_identities == [
        (session.rodex_uuid, database),
        (session.rodex_uuid, database),
    ]
    assert launcher.configured[0].tmux_session_name == "automatic-beluga"
    assert launcher.attached == []
    assert lookup_rodex_uuid_from_an_id(session.id, database) == session.rodex_uuid
    assert (
        lookup_codex_uuid_from_a_rodex_session_id(session.id, database)
        == REPLACEMENT_CODEX_UUID
    )
    assert output == {
        "status": "running",
        "rodex_session_name": "automatic-beluga",
        "rodex_session_uuid": str(session.rodex_uuid),
        "codex_session_uuid": str(REPLACEMENT_CODEX_UUID),
    }


def test_unknown_underscore_command_passes_through_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    delegator = RecordingCodexDelegator()
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    arguments = ["_future-command", "argument"]

    assert run(arguments, codex_delegator=delegator) == 0

    assert delegator.calls == [("/usr/bin/codex", arguments)]


def test_existing_name_wins_over_a_possible_future_codex_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "future-command"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_a_rodex_session(
        database,
        codex_session_uuid=CODEX_UUID,
        user_identity=RodexSessionsUserIdentity(1009, 1010, "dna"),
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="rodex-token",
    )
    launcher = StubLauncher(tmp_path)
    delegator = RecordingCodexDelegator()

    assert (
        run(
            ["future-command"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
            codex_delegator=delegator,
        )
        == 0
    )

    assert delegator.calls == []
    assert launcher.started == []
    assert launcher.attached[0].tmux_session_name == "future-command"


def test_live_cool_name_argument_renames_configures_and_reattaches_without_starting_codex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug",
        lambda word_count: "automatic-beluga" if word_count == 2 else "unused-name-here",
    )
    create_a_rodex_session(
        database,
        codex_session_uuid=CODEX_UUID,
        user_identity=RodexSessionsUserIdentity(1009, 1010, "dna"),
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="rodex-token",
    )
    launcher = StubLauncher(tmp_path)
    monkeypatch.setattr(
        "rodex.cli.shutil.which",
        lambda command: None if command == "codex" else available_prerequisite(command),
    )

    assert (
        run(
            ["automatic-beluga"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )
        == 0
    )

    assert launcher.started == []
    assert launcher.existing_checks == [
        LiveTmuxSession(tmp_path / "tmux.sock", "rodex-token")
    ]
    assert len(launcher.renamed) == 1
    assert launcher.renamed[0][0].tmux_session_name == "rodex-token"
    assert launcher.renamed[0][1] == "automatic-beluga"
    assert launcher.configured[0].tmux_session_name == "automatic-beluga"
    assert launcher.attached == launcher.configured
    tmux_link = lookup_rodex_tmux_session(1, database)
    assert tmux_link is not None
    assert tmux_link.tmux_session_name == "automatic-beluga"
    assert "Reattaching Rodex automatic-beluga" in capsys.readouterr().out


def test_ended_cool_name_argument_transparently_resumes_its_codex_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug",
        lambda word_count: "automatic-beluga" if word_count == 2 else "unused-name-here",
    )
    create_a_rodex_session(
        database,
        codex_session_uuid=CODEX_UUID,
        user_identity=RodexSessionsUserIdentity(1009, 1010, "dna"),
        tmux_server_socket_path=tmp_path / "stale.sock",
        tmux_session_name="automatic-beluga",
    )
    launcher = StubLauncher(tmp_path)
    launcher.live = False
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)

    assert (
        run(
            ["automatic-beluga"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )
        == 0
    )

    assert launcher.started == [(Path.cwd(), ["resume", str(CODEX_UUID)])]
    assert launcher.renamed == [(launcher.runtime, "automatic-beluga")]
    assert launcher.attached[0].tmux_session_name == "automatic-beluga"
    tmux_link = lookup_rodex_tmux_session(1, database)
    assert tmux_link is not None
    assert tmux_link.tmux_server_socket_path == str(
        launcher.runtime.tmux_server_socket_path
    )
    assert tmux_link.tmux_session_name == "automatic-beluga"
    assert f"Resumed Rodex automatic-beluga -> Codex {CODEX_UUID}" in (
        capsys.readouterr().out
    )


def test_unsaved_codex_session_starts_fresh_and_relinks_the_rodex_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    session = create_a_rodex_session(
        database,
        codex_session_uuid=CODEX_UUID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "stale.sock",
        tmux_session_name="automatic-beluga",
    )
    launcher = StubLauncher(tmp_path)
    launcher.live = False
    launcher.start_error = RodexCodexSessionNotFoundError(
        f"Codex has no saved session for exact identity {CODEX_UUID}"
    )
    launcher.observed_codex_uuid = REPLACEMENT_CODEX_UUID

    assert (
        run(
            ["automatic-beluga"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )
        == 0
    )

    assert lookup_rodex_uuid_from_an_id(session.id, database) == session.rodex_uuid
    assert (
        lookup_codex_uuid_from_a_rodex_session_id(session.id, database)
        == REPLACEMENT_CODEX_UUID
    )
    assert launcher.started == [
        (Path.cwd(), ["resume", str(CODEX_UUID)]),
        (Path.cwd(), []),
    ]
    assert launcher.renamed == [(launcher.runtime, "automatic-beluga")]
    assert launcher.configured[0].tmux_session_name == "automatic-beluga"
    assert launcher.attached == launcher.configured
    assert launcher.stopped == []
    tmux_link = lookup_rodex_tmux_session(session.id, database)
    assert tmux_link is not None
    assert tmux_link.tmux_server_socket_path == str(
        launcher.runtime.tmux_server_socket_path
    )
    assert f"Recovered Rodex automatic-beluga -> Codex {REPLACEMENT_CODEX_UUID}" in (
        capsys.readouterr().out
    )


def test_non_missing_history_resume_failure_does_not_start_a_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_a_rodex_session(
        database,
        codex_session_uuid=CODEX_UUID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "stale.sock",
        tmux_session_name="automatic-beluga",
    )
    launcher = StubLauncher(tmp_path)
    launcher.live = False
    launcher.start_error = RodexRuntimeError("Codex transport failed")
    recorded_tmux = lookup_rodex_tmux_session(1, database)

    with pytest.raises(RodexLaunchError, match="Codex transport failed"):
        run(
            ["automatic-beluga"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )

    assert lookup_codex_uuid_from_a_rodex_session_id(1, database) == CODEX_UUID
    assert lookup_rodex_tmux_session(1, database) == recorded_tmux
    assert launcher.started == [(Path.cwd(), ["resume", str(CODEX_UUID)])]


def test_unsaved_session_remains_linked_to_the_old_uuid_if_recovery_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_a_rodex_session(
        database,
        codex_session_uuid=CODEX_UUID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "stale.sock",
        tmux_session_name="automatic-beluga",
    )
    launcher = StubLauncher(tmp_path)
    launcher.live = False
    launcher.start_errors = [
        RodexCodexSessionNotFoundError(
            f"Codex has no saved session for exact identity {CODEX_UUID}"
        ),
        RodexRuntimeError("fresh Codex startup failed"),
    ]
    recorded_tmux = lookup_rodex_tmux_session(1, database)

    with pytest.raises(RodexLaunchError, match="replacement Codex runtime") as raised:
        run(
            ["automatic-beluga"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )

    assert "fresh Codex startup failed" in str(raised.value)
    assert lookup_codex_uuid_from_a_rodex_session_id(1, database) == CODEX_UUID
    assert lookup_rodex_tmux_session(1, database) == recorded_tmux
    assert launcher.started == [
        (Path.cwd(), ["resume", str(CODEX_UUID)]),
        (Path.cwd(), []),
    ]
    assert launcher.renamed == []
    assert launcher.configured == []
    assert launcher.attached == []


@pytest.mark.parametrize("lookup_name", ["black-sawfly", "work"])
def test_either_name_route_displays_the_user_defined_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    lookup_name: str,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug",
        lambda _word_count: "black-sawfly",
    )
    create_a_rodex_session(
        database,
        codex_session_uuid=CODEX_UUID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="black-sawfly",
    )
    assign_a_user_defined_cool_name("black-sawfly", "work", database, user_identity=DNA)
    launcher = StubLauncher(tmp_path)
    monkeypatch.setattr(
        "rodex.cli.shutil.which",
        lambda command: None if command == "codex" else available_prerequisite(command),
    )

    assert run([lookup_name], database_path=database, launcher=launcher) == 0  # type: ignore[arg-type]

    assert launcher.renamed == [
        (LiveTmuxSession(tmp_path / "tmux.sock", "black-sawfly"), "work")
    ]
    assert launcher.configured == [LiveTmuxSession(tmp_path / "tmux.sock", "work")]
    tmux_link = lookup_rodex_tmux_session(1, database)
    assert tmux_link is not None
    assert tmux_link.tmux_session_name == "work"
    assert "Reattaching Rodex work" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["_alias"])
@pytest.mark.parametrize("force_flag", ["--force"])
def test_alias_command_accepts_force_without_starting_codex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    force_flag: str,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug",
        lambda _word_count: "black-sawfly",
    )
    monkeypatch.setattr(
        "rodex_functions.sessions.current_rodex_sessions_user_identity", lambda: DNA
    )
    monkeypatch.setattr(
        "rodex.cli.shutil.which",
        lambda name: None if name == "codex" else available_prerequisite(name),
    )
    create_a_rodex_session(
        database,
        codex_session_uuid=CODEX_UUID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="black-sawfly",
    )
    launcher = StubLauncher(tmp_path)

    assert (
        run(
            [command, "black-sawfly", "first"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )
        == 0
    )
    assert (
        run(
            [command, force_flag, "black-sawfly", "replacement"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )
        == 0
    )

    assert launcher.started == []
    assert [name for _, name in launcher.renamed] == ["first", "replacement"]
    assert launcher.configured[-1].tmux_session_name == "replacement"
    tmux_link = lookup_rodex_tmux_session(1, database)
    assert tmux_link is not None
    assert tmux_link.tmux_session_name == "replacement"
    assert "Rodex name: replacement" in capsys.readouterr().out


def test_alias_replacement_without_force_is_reported_on_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug",
        lambda _word_count: "black-sawfly",
    )
    monkeypatch.setattr(
        "rodex_functions.sessions.current_rodex_sessions_user_identity", lambda: DNA
    )
    monkeypatch.setattr(
        "rodex.cli.shutil.which",
        lambda name: None if name == "codex" else available_prerequisite(name),
    )
    create_a_rodex_session(
        database,
        codex_session_uuid=CODEX_UUID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="black-sawfly",
    )
    assign_a_user_defined_cool_name("black-sawfly", "first", database, user_identity=DNA)
    monkeypatch.setenv("RODEX_DATABASE_PATH", str(database))
    monkeypatch.setattr(sys, "argv", ["rodex", "_alias", "black-sawfly", "replacement"])

    with pytest.raises(SystemExit) as raised:
        main()

    assert raised.value.code == 1
    assert "use --force" in capsys.readouterr().err


@pytest.mark.parametrize("unsupported_force", ["-f", "--f", "-force"])
def test_alias_rejects_removed_force_spellings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsupported_force: str,
) -> None:
    launcher = StubLauncher(tmp_path)
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)

    with pytest.raises(RodexLaunchError, match="unknown alias option"):
        run(
            ["_alias", unsupported_force, "session", "replacement"],
            database_path=tmp_path / "rodex.sqlite3",
            launcher=launcher,  # type: ignore[arg-type]
        )


def test_empty_alias_is_a_concise_stderr_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _word_count: "safe-name"
    )
    monkeypatch.setattr(
        "rodex_functions.sessions.current_rodex_sessions_user_identity", lambda: DNA
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_a_rodex_session(
        database,
        codex_session_uuid=CODEX_UUID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="safe-name",
    )
    monkeypatch.setenv("RODEX_DATABASE_PATH", str(database))
    monkeypatch.setattr(sys, "argv", ["rodex", "_alias", "safe-name", ""])

    with pytest.raises(SystemExit) as raised:
        main()

    assert raised.value.code == 1
    error_output = capsys.readouterr().err
    assert error_output.startswith("rodex: ")
    assert "non-empty" in error_output
    assert "Traceback" not in error_output


def test_sqlite_operational_error_is_a_concise_stderr_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "rodex.cli.run",
        lambda: (_ for _ in ()).throw(sqlite3.OperationalError("database unavailable")),
    )

    with pytest.raises(SystemExit) as raised:
        main()

    assert raised.value.code == 1
    assert capsys.readouterr().err == "rodex: database unavailable\n"


def test_missing_executable_retains_command_not_found_exit_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "rodex.cli.run",
        lambda: (_ for _ in ()).throw(
            RodexExecutableNotFoundError("tmux executable was not found: tmux")
        ),
    )

    with pytest.raises(SystemExit) as raised:
        main()

    assert raised.value.code == 127
    assert "tmux executable" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["_running"])
def test_running_commands_show_only_the_current_users_live_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug",
        lambda _word_count: "black-sawfly",
    )
    create_a_rodex_session(
        database,
        codex_session_uuid=CODEX_UUID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "dna.sock",
        tmux_session_name="black-sawfly",
    )
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug",
        lambda _word_count: "silver-otter",
    )
    create_a_rodex_session(
        database,
        codex_session_uuid=uuid.UUID(int=CODEX_UUID.int + 1),
        user_identity=RodexSessionsUserIdentity(2001, 2002, "other"),
        tmux_server_socket_path=tmp_path / "other.sock",
        tmux_session_name="silver-otter",
    )
    assign_a_user_defined_cool_name("black-sawfly", "work", database, user_identity=DNA)
    monkeypatch.setattr(
        "rodex_functions.sessions.current_rodex_sessions_user_identity", lambda: DNA
    )
    monkeypatch.setattr(
        "rodex.cli.shutil.which",
        lambda name: None if name == "codex" else available_prerequisite(name),
    )
    launcher = StubLauncher(tmp_path)

    assert run([command], database_path=database, launcher=launcher) == 0  # type: ignore[arg-type]

    assert launcher.started == []
    assert launcher.existing_checks == [
        LiveTmuxSession(tmp_path / "dna.sock", "black-sawfly")
    ]
    output = capsys.readouterr().out
    assert "Running Rodex sessions: 1" in output
    assert f"work -> Codex {CODEX_UUID}" in output
    assert "black-sawfly" not in output
    assert "silver-otter" not in output


@pytest.mark.parametrize("arguments", [[], ["_create"]], ids=["bare", "explicit"])
def test_new_launch_cleans_up_the_renamed_runtime_when_persistence_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    database = tmp_path / "rodex.sqlite3"
    launcher = StubLauncher(tmp_path)
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _word_count: "safe-name"
    )
    monkeypatch.setattr(
        "rodex.cli.update_rodex_tmux_session_name",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("persist failed")),
    )

    with pytest.raises(RuntimeError, match="persist failed"):
        run(arguments, database_path=database, launcher=launcher)  # type: ignore[arg-type]

    assert launcher.renamed == [(launcher.runtime, "safe-name")]
    assert launcher.stopped == [
        (replace(launcher.runtime, tmux_session_name="safe-name"), False)
    ]
    assert launcher.attached == []


def test_live_reattach_restores_the_recorded_name_when_persistence_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _word_count: "safe-name"
    )
    create_a_rodex_session(
        database,
        codex_session_uuid=CODEX_UUID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="rodex-token",
    )
    launcher = StubLauncher(tmp_path)
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(
        "rodex.cli.update_rodex_tmux_session_name",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("persist failed")),
    )

    with pytest.raises(RuntimeError, match="persist failed"):
        run(["safe-name"], database_path=database, launcher=launcher)  # type: ignore[arg-type]

    assert [name for _, name in launcher.renamed] == ["safe-name", "rodex-token"]
    tmux_link = lookup_rodex_tmux_session(1, database)
    assert tmux_link is not None
    assert tmux_link.tmux_session_name == "rodex-token"
    assert launcher.attached == []


def test_alias_rename_failure_preserves_the_previous_name_everywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _word_count: "safe-name"
    )
    monkeypatch.setattr(
        "rodex_functions.sessions.current_rodex_sessions_user_identity", lambda: DNA
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_a_rodex_session(
        database,
        codex_session_uuid=CODEX_UUID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="safe-name",
    )
    launcher = StubLauncher(tmp_path)
    assert (
        run(
            ["_alias", "safe-name", "first"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )
        == 0
    )

    def fail_rename(*args: object, **kwargs: object) -> LiveTmuxSession:
        raise RodexRuntimeError("rename failed")

    monkeypatch.setattr(launcher, "rename", fail_rename)
    with pytest.raises(RodexRuntimeError, match="rename failed"):
        run(
            ["_alias", "--force", "safe-name", "replacement"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )

    names = lookup_rodex_session_names(1, database)
    assert names is not None
    assert names.display_name == "first"
    assert lookup_rodex_session_id_from_a_cool_name("replacement", database) is None
    tmux_link = lookup_rodex_tmux_session(1, database)
    assert tmux_link is not None
    assert tmux_link.tmux_session_name == "first"


def test_alias_database_failure_renames_tmux_back_and_leaves_no_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _word_count: "safe-name"
    )
    monkeypatch.setattr(
        "rodex_functions.sessions.current_rodex_sessions_user_identity", lambda: DNA
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_a_rodex_session(
        database,
        codex_session_uuid=CODEX_UUID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="safe-name",
    )
    launcher = StubLauncher(tmp_path)
    monkeypatch.setattr(
        "rodex_functions.sessions.reserve_specific_cool_name",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("database failed")
        ),
    )

    with pytest.raises(sqlite3.OperationalError, match="database failed"):
        run(
            ["_alias", "safe-name", "work"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )

    assert [name for _, name in launcher.renamed] == ["work", "safe-name"]
    names = lookup_rodex_session_names(1, database)
    assert names is not None
    assert names.user_defined_cool_name is None
    assert lookup_rodex_session_id_from_a_cool_name("work", database) is None
    tmux_link = lookup_rodex_tmux_session(1, database)
    assert tmux_link is not None
    assert tmux_link.tmux_session_name == "safe-name"


def test_concurrent_alias_commands_serialize_across_tmux_and_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _word_count: "safe-name"
    )
    monkeypatch.setattr(
        "rodex_functions.sessions.current_rodex_sessions_user_identity", lambda: DNA
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_a_rodex_session(
        database,
        codex_session_uuid=CODEX_UUID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="safe-name",
    )
    state_lock = Lock()
    live_name = ["safe-name"]
    first_rename_started = Event()
    allow_first_commit = Event()
    second_attempt_started = Event()
    second_finished = Event()

    class ConcurrentAliasLauncher:
        def __init__(self, *, pause_after_rename: bool) -> None:
            self.pause_after_rename = pause_after_rename

        def session_exists(self, runtime: LiveTmuxSession) -> bool:
            with state_lock:
                return runtime.tmux_session_name == live_name[0]

        def rename(
            self, runtime: LiveTmuxSession, tmux_session_name: str
        ) -> LiveTmuxSession:
            with state_lock:
                assert runtime.tmux_session_name == live_name[0]
                live_name[0] = tmux_session_name
            if self.pause_after_rename:
                first_rename_started.set()
                assert allow_first_commit.wait(5)
            return replace(runtime, tmux_session_name=tmux_session_name)

        def configure_identity_status(self, runtime: LiveTmuxSession) -> None:
            return None

    first_errors: list[BaseException] = []
    second_errors: list[BaseException] = []

    def assign_name(
        name: str,
        launcher: ConcurrentAliasLauncher,
        errors: list[BaseException],
        finished: Event | None = None,
    ) -> None:
        try:
            run(
                ["_alias", "safe-name", name],
                database_path=database,
                launcher=launcher,  # type: ignore[arg-type]
            )
        except BaseException as error:
            errors.append(error)
        finally:
            if finished is not None:
                finished.set()

    first_thread = Thread(
        target=assign_name,
        args=("first", ConcurrentAliasLauncher(pause_after_rename=True), first_errors),
    )
    second_thread = Thread(
        target=assign_name,
        args=(
            "second",
            ConcurrentAliasLauncher(pause_after_rename=False),
            second_errors,
            second_finished,
        ),
    )

    def observe_assignment_attempt(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if current_thread() is second_thread:
            second_attempt_started.set()
        return open_a_user_defined_cool_name_assignment(*args, **kwargs)

    monkeypatch.setattr(
        "rodex.cli.open_a_user_defined_cool_name_assignment",
        observe_assignment_attempt,
    )
    first_thread.start()
    assert first_rename_started.wait(5)
    second_thread.start()
    assert second_attempt_started.wait(5)
    try:
        second_was_serialized = not second_finished.wait(0.25)
    finally:
        allow_first_commit.set()
    assert second_was_serialized
    first_thread.join(5)
    second_thread.join(5)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert first_errors == []
    assert len(second_errors) == 1
    assert isinstance(second_errors[0], RodexSessionError)
    assert "use --force" in str(second_errors[0])
    names = lookup_rodex_session_names(1, database)
    assert names is not None
    assert names.display_name == "first"
    tmux_link = lookup_rodex_tmux_session(1, database)
    assert tmux_link is not None
    assert tmux_link.tmux_session_name == "first"
    assert live_name == ["first"]


def test_named_open_rejects_a_session_owned_by_another_posix_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _word_count: "other-work"
    )
    create_a_rodex_session(
        database,
        codex_session_uuid=CODEX_UUID,
        user_identity=RodexSessionsUserIdentity(2001, 2002, "other"),
        tmux_server_socket_path=tmp_path / "other.sock",
        tmux_session_name="other-work",
    )
    monkeypatch.setattr(
        "rodex_functions.sessions.current_rodex_sessions_user_identity", lambda: DNA
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    launcher = StubLauncher(tmp_path)

    with pytest.raises(RodexSessionError, match="not owned"):
        run(["other-work"], database_path=database, launcher=launcher)  # type: ignore[arg-type]

    assert launcher.started == []
    assert launcher.existing_checks == []
    assert launcher.attached == []


def test_resume_stops_new_runtime_if_codex_reports_a_different_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug",
        lambda word_count: "automatic-beluga" if word_count == 2 else "unused-name-here",
    )
    create_a_rodex_session(
        database,
        codex_session_uuid=CODEX_UUID,
        tmux_server_socket_path=tmp_path / "stale.sock",
        tmux_session_name="automatic-beluga",
    )
    launcher = StubLauncher(tmp_path)
    launcher.live = False
    launcher.observed_codex_uuid = uuid.UUID(int=CODEX_UUID.int + 1)
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)

    with pytest.raises(RodexLaunchError, match="unexpected session"):
        run(
            ["automatic-beluga"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )

    assert launcher.stopped == [(launcher.runtime, False)]
    assert lookup_codex_uuid_from_a_rodex_session_id(1, database) == CODEX_UUID
    tmux_link = lookup_rodex_tmux_session(1, database)
    assert tmux_link is not None
    assert tmux_link.tmux_server_socket_path == str(tmp_path / "stale.sock")
    assert launcher.renamed == []
    assert launcher.configured == []
    assert launcher.attached == []


@pytest.mark.parametrize(
    ("missing", "message"),
    [("codex", "Codex executable"), ("tmux", "tmux executable")],
)
@pytest.mark.parametrize("arguments", [[], ["_create"]], ids=["bare", "explicit"])
def test_run_does_not_create_a_session_when_a_prerequisite_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
    message: str,
    arguments: list[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "rodex.cli.shutil.which",
        lambda command: None if command == missing else f"/usr/bin/{command}",
    )

    with pytest.raises(RodexLaunchError, match=message):
        run(arguments, database_path=database)

    assert not database.exists()


@pytest.mark.parametrize("arguments", [[], ["_create"]], ids=["bare", "explicit"])
def test_database_failure_stops_the_unregistered_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    launcher = StubLauncher(tmp_path)
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(
        "rodex.cli.create_a_rodex_session",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("database failed")),
    )

    with pytest.raises(RuntimeError, match="database failed"):
        run(
            arguments,
            database_path=tmp_path / "db.sqlite3",
            launcher=launcher,  # type: ignore[arg-type]
        )

    assert launcher.stopped == [(launcher.runtime, False)]
    assert launcher.attached == []


def test_project_root_launcher_is_executable_and_uses_the_project_environment() -> None:
    project_root = Path(__file__).parents[1]
    launcher = project_root / "rodex"

    assert os.access(launcher, os.X_OK)
    contents = launcher.read_text(encoding="utf-8")
    assert 'uv run --project "$RODEX_PROJECT_DIR" rodex "$@"' in contents
