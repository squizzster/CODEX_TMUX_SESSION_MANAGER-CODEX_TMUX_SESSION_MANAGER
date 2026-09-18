"""Human-readable process titles for attached Rodex clients."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import setproctitle


def session_process_title(display_name: str) -> str:
    """Return the complete process title derived from one Rodex display name."""
    return f"rodex_{display_name.replace('-', '_')}"


@contextmanager
def rodex_session_process_title(display_name: str) -> Iterator[str]:
    """Expose one session's complete identity while this client is attached."""
    previous_title = setproctitle.getproctitle()
    title = session_process_title(display_name)
    setproctitle.setproctitle(title)
    try:
        yield title
    finally:
        setproctitle.setproctitle(previous_title)
