"""Validation gates for environment-dependent primary workflow tests."""

import os
import pwd

import pytest

from rodex_registry import RodexSessionsUserIdentity


@pytest.fixture
def executing_user() -> RodexSessionsUserIdentity:
    """Match session provenance to the user owning real test files and processes."""
    return RodexSessionsUserIdentity(os.getuid(), os.getgid(), pwd.getpwuid(os.getuid()).pw_name)


@pytest.fixture
def foreign_user(executing_user: RodexSessionsUserIdentity) -> RodexSessionsUserIdentity:
    """Synthetic registry owner that cannot coincide with the executing user."""
    return RodexSessionsUserIdentity(executing_user.uid + 1, executing_user.gid + 1, "foreign-test-user")


def pytest_addoption(parser):
    parser.addoption(
        "--require-live-startup",
        action="store_true",
        help="Fail instead of skipping when real Rodex startup prerequisites are missing.",
    )
