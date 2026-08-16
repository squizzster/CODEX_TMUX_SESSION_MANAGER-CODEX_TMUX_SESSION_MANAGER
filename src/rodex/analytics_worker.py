"""Executable entry point for the fail-open Rodex analytics sidecar."""

from .analytics import analytics_worker_main

if __name__ == "__main__":
    raise SystemExit(analytics_worker_main())
