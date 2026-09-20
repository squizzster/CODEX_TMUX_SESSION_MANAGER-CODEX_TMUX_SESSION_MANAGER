"""Rodex command-line launcher."""


def main() -> None:
    """Load the full CLI only when the installed command is invoked."""
    import sys

    from .installation import RodexInstallationError, enter_fixed_installation

    try:
        enter_fixed_installation()
    except (RodexInstallationError, OSError) as error:
        print(f"rodex: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    from .cli import main as cli_main

    cli_main()


__all__ = ["main"]
