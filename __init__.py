"""Hermes plugin registration for Document Reader."""

from __future__ import annotations


def register(ctx) -> None:
    from .cli import handle_command, setup_parser

    ctx.register_cli_command(
        name="document-reader",
        help="Install and manage the profile-scoped Document Reader service",
        description=(
            "Guarded install, status, recovery, rollback, and uninstall for the "
            "Document Reader instance owned by the currently selected Hermes profile."
        ),
        setup_fn=setup_parser,
        handler_fn=handle_command,
    )
