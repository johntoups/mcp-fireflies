"""Credential management for Fireflies API.

Supports cascading credential lookup:
1. Explicit value (passed at runtime)
2. Environment variable (FIREFLIES_API_KEY)
3. OS keychain (via keyring library)
"""

import os
import sys

import click
import keyring

SERVICE_NAME = "mcp-fireflies"
ACCOUNT_NAME = "api_key"
ENV_VAR = "FIREFLIES_API_KEY"


def get_api_key(explicit_key: str | None = None) -> str | None:
    """Get API key from cascading sources.

    Args:
        explicit_key: Explicitly provided key (highest priority)

    Returns:
        API key if found, None otherwise
    """
    # 1. Explicit value
    if explicit_key:
        return explicit_key

    # 2. Environment variable
    env_key = os.environ.get(ENV_VAR)
    if env_key:
        return env_key

    # 3. OS keychain
    try:
        keychain_key = keyring.get_password(SERVICE_NAME, ACCOUNT_NAME)
        if keychain_key:
            return keychain_key
    except keyring.errors.KeyringError:
        pass  # Keyring not available

    return None


def store_api_key(api_key: str) -> bool:
    """Store API key in OS keychain.

    Args:
        api_key: The API key to store

    Returns:
        True if successful, False otherwise
    """
    try:
        keyring.set_password(SERVICE_NAME, ACCOUNT_NAME, api_key)
        return True
    except keyring.errors.KeyringError as e:
        click.echo(f"Failed to store in keychain: {e}", err=True)
        return False


def delete_api_key() -> bool:
    """Delete API key from OS keychain.

    Returns:
        True if successful, False otherwise
    """
    try:
        keyring.delete_password(SERVICE_NAME, ACCOUNT_NAME)
        return True
    except keyring.errors.PasswordDeleteError:
        return False  # Key didn't exist
    except keyring.errors.KeyringError as e:
        click.echo(f"Failed to delete from keychain: {e}", err=True)
        return False


@click.group()
def cli():
    """Manage Fireflies API credentials."""
    pass


@cli.command()
@click.option("--key", prompt="Fireflies API Key", hide_input=True,
              help="Your Fireflies API key (from fireflies.ai/integrations)")
def store(key: str):
    """Store API key in system keychain."""
    if store_api_key(key):
        click.echo("API key stored in system keychain.")
    else:
        click.echo("Failed to store API key. Set FIREFLIES_API_KEY env var instead.", err=True)
        sys.exit(1)


@cli.command()
def show():
    """Show where API key is configured (not the key itself)."""
    if os.environ.get(ENV_VAR):
        click.echo(f"API key found in environment variable: {ENV_VAR}")
    elif keyring.get_password(SERVICE_NAME, ACCOUNT_NAME):
        click.echo("API key found in system keychain.")
    else:
        click.echo("No API key configured.")
        click.echo(f"\nTo configure, either:")
        click.echo(f"  1. Run: fireflies-auth store")
        click.echo(f"  2. Set environment variable: export {ENV_VAR}=your_key")
        sys.exit(1)


@cli.command()
def delete():
    """Delete API key from system keychain."""
    if delete_api_key():
        click.echo("API key deleted from system keychain.")
    else:
        click.echo("No API key found in keychain.")


if __name__ == "__main__":
    cli()
