"""Rodex command-line failure taxonomy shared by command domains."""

from __future__ import annotations


class RodexLaunchError(RuntimeError):
    """Rodex could not complete a requested launcher operation."""


class ExactRuntimeIdentityRequiredError(RodexLaunchError):
    """A live endpoint lacks durable incarnation authority for mutation."""


class RodexExecutableNotFoundError(RodexLaunchError):
    """A required executable could not be resolved from PATH."""
