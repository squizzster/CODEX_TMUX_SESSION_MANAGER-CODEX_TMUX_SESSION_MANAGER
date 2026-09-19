"""Regressions for per-runtime inputs and persistent/shared resource boundaries."""

import json
import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier

import pytest
from runtime_peer_fixtures import TEST_PEER
from test_terminal_gateway import outer_terminal, read_until
from websockets.sync.client import unix_connect

import rodex_sql.transactions as transactions
from rodex.interaction_pipeline import SessionInteractionPipeline
from rodex.protocol_proxy import CodexProtocolEventTap
from rodex.terminal_gateway import TerminalSessionGateway
from rodex_registry import RodexSessionError, audit_rodex_database_integrity, initialise_rodex_database
from rodex_registry.schema import _normalise_schema_sql
from rodex_sql import open_rodex_bootstrap_transaction, open_rodex_read_transaction, open_rodex_transaction


def marker_database(path):
    with open_rodex_bootstrap_transaction(path) as connection:
        connection.execute("CREATE TABLE marker (value TEXT)")


def test_overlapping_catalogs_retain_each_active_database(tmp_path):
    first, second = tmp_path / "first.sqlite3", tmp_path / "second.sqlite3"
    marker_database(first)
    marker_database(second)
    with open_rodex_transaction(first) as a:
        a.execute("INSERT INTO marker VALUES ('A')")
        with open_rodex_transaction(second) as b:
            b.execute("INSERT INTO marker VALUES ('B')")
            with open_rodex_read_transaction(first) as a_read:
                assert a_read.execute("SELECT count(*) FROM marker").fetchone() == (0,)
    for path, expected in ((first, "A"), (second, "B")):
        with open_rodex_read_transaction(path) as connection:
            assert connection.execute("SELECT value FROM marker").fetchall() == [(expected,)]


def test_concurrent_catalogs_preserve_commit_and_rollback(tmp_path):
    paths = [tmp_path / f"catalog-{index}.sqlite3" for index in range(4)]
    for path in paths:
        marker_database(path)
    barrier = Barrier(len(paths))

    def write(path):
        with open_rodex_transaction(path) as connection:
            connection.execute("INSERT INTO marker VALUES ('committed')")
            barrier.wait(timeout=3)
        with pytest.raises(RuntimeError, match="rollback"), open_rodex_transaction(path) as connection:
            connection.execute("INSERT INTO marker VALUES ('discarded')")
            raise RuntimeError("rollback")
        with open_rodex_read_transaction(path) as connection:
            assert connection.execute("SELECT value FROM marker").fetchall() == [("committed",)]

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, paths))
    transactions._close_process_wal_lifetime_owner()


@pytest.mark.parametrize("changed", ["'INTEGER'", "'inte  ger'", "'integer IF NOT EXISTS '"])
def test_schema_audit_rejects_changed_literal_semantics(tmp_path, changed):
    path = tmp_path / "rodex.sqlite3"
    initialise_rodex_database(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_master SET sql=replace(sql, ?, ?) WHERE name='rodex_sessions'",
            ("'integer'", changed),
        )
        connection.commit()
    with pytest.raises(RodexSessionError, match=r"definition|catalog|constraints"):
        audit_rodex_database_integrity(path)


@pytest.mark.parametrize("literal", ["'a  b'", "'a IF NOT EXISTS b'", "'it''s case Sensitive'", '"Mixed Name"'])
def test_schema_normalization_preserves_quoted_bytes(literal):
    assert literal in _normalise_schema_sql(f"CREATE TABLE IF NOT EXISTS example(x CHECK(x={literal}));")


def test_idle_websocket_disconnect_releases_all_subscribers(tmp_path):
    tap = CodexProtocolEventTap(tmp_path / "events.sock", peer_identity=TEST_PEER)
    tap.start()
    try:
        for _ in range(8):
            with unix_connect(str(tmp_path / "events.sock"), additional_headers=TEST_PEER.headers()) as connection:
                assert "activeTurns" in json.loads(connection.recv(timeout=1))["params"]
        deadline = time.monotonic() + 1.5
        while tap._subscribers and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not tap._subscribers
    finally:
        tap.close()


@pytest.mark.parametrize("directory", ["project-a", "project-b"])
def test_native_gateway_uses_explicit_workspace_and_relative_arguments(tmp_path, directory):
    workspace = tmp_path / directory
    workspace.mkdir()
    (workspace / "relative.txt").write_text(directory)
    script = (
        "import os,pathlib; print('CWD='+os.getcwd(),flush=True); "
        "print(pathlib.Path('relative.txt').read_text(),flush=True)"
    )
    with outer_terminal() as (master, slave):
        gateway = TerminalSessionGateway(
            [sys.executable, "-I", "-c", script],
            env={**os.environ, "PWD": str(workspace)},
            cwd=workspace,
            pipeline=SessionInteractionPipeline(),
            runtime_identity="cwd-test",
            registrations=(),
            confirm_native_prefix=lambda _: True,
            input_fd=slave,
            output_fd=slave,
        )
        try:
            output = read_until(gateway, master, ("CWD=" + str(workspace)).encode())
            assert directory.encode() in output
            assert gateway.wait(timeout=2) == 0
        finally:
            gateway.close()


def test_fork_preserves_active_parent_borrow_and_rejects_new_child_sql(tmp_path):
    path = tmp_path / "fork.sqlite3"
    marker_database(path)
    with open_rodex_transaction(path) as connection:
        connection.execute("INSERT INTO marker VALUES ('parent')")
        child = os.fork()
        if child == 0:
            # A child of an active SQL fork must exec or _exit, never unwind or
            # reuse inherited SQLite connections. New acquisitions fail closed.
            try:
                with open_rodex_read_transaction(path):
                    pass
            except transactions.RodexSQLError:
                os._exit(0)
            os._exit(1)
        _, status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(status) == 0
    with open_rodex_read_transaction(path) as connection:
        assert connection.execute("SELECT value FROM marker").fetchall() == [("parent",)]


def test_analyzer_import_failure_does_not_block_help_or_native_delegation(tmp_path):
    source = """
import importlib.abc
import sys
class BlockAnalyzer(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith("codex_protocol_log_analyzer"):
            raise ImportError("deliberately unavailable analyzer")
sys.meta_path.insert(0, BlockAnalyzer())
from rodex.cli import run
assert run(["_help"]) == 0
assert run(["--version"], codex_delegator=lambda *args: 23) == 23
assert "rodex.analytics_analyzer" not in sys.modules
assert "rodex.analytics" not in sys.modules
"""
    result = subprocess.run([sys.executable, "-I", "-c", source], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
