"""CLI entry point for gamdl web UI."""

import sys


def _check_dependencies():
    """Check that required dependencies are installed using only stdlib."""
    required = ["click", "httpx", "fastapi", "uvicorn"]
    missing = []
    for module in required:
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if missing:
        print("")
        print("=" * 60)
        print("  Looks like you're missing some dependencies!")
        print(f"  Missing: {', '.join(missing)}")
        print("")
        print("  Run this command to install them:")
        print("")
        print('    python -m pip install -e ".[web]"')
        print("")
        print("  Then try launching again.")
        print("=" * 60)
        print("")
        sys.exit(1)


_check_dependencies()

import click


@click.command()
@click.option(
    "--host",
    default="0.0.0.0",
    help="Host to bind the server to",
    show_default=True,
)
@click.option(
    "--port",
    default=8080,  # Change default port here
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
