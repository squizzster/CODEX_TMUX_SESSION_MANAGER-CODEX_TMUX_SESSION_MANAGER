"""Rodex command-line launcher."""


def main() -> None:
    """Load the full CLI only when the installed command is invoked."""
    from .cli import main as cli_main

    cli_main()


__all__ = ["main"]
