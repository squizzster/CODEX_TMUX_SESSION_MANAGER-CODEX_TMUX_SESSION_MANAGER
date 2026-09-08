"""Validation gates for environment-dependent primary workflow tests."""


def pytest_addoption(parser):
    parser.addoption(
        "--require-live-startup",
        action="store_true",
        help="Fail instead of skipping when real Rodex startup prerequisites are missing.",
    )
