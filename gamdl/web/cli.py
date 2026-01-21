"""CLI entry point for gamdl web UI."""

import click


@click.command()
@click.option(
    "--host",
    default="127.0.0.1",
    help="Host to bind the server to",
    show_default=True,
)
@click.option(
    "--port",
    default=8080,
    help="Port to bind the server to",
    show_default=True,
    type=int,
)
@click.option(
    "--advanced",
    "-adv",
    is_flag=True,
    help="Launch advanced UI with library browser",
)
def main(host: str, port: int, advanced: bool):
    """Start the gamdl web UI server."""
    if advanced:
        click.echo("Starting gamdl advanced web UI with library browser...")
        from gamdl.web.server_advanced import main as server_advanced_main
        server_advanced_main(host=host, port=port)
    else:
        click.echo("Starting gamdl basic web UI...")
        from gamdl.web.server import main as server_main
        server_main(host=host, port=port)


if __name__ == "__main__":
    main()
