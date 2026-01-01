"""CLI commands for Fireflies MCP."""

import click

from .auth import get_api_key, store_api_key, delete_api_key


@click.group()
def auth():
    """Manage Fireflies API credentials."""
    pass


@auth.command("store")
@click.option("--key", prompt="Fireflies API Key", hide_input=True, help="API key to store")
def auth_store(key: str):
    """Store Fireflies API key in system keychain."""
    try:
        store_api_key(key)
        click.echo("API key stored successfully in system keychain.")
    except Exception as e:
        click.echo(f"Error storing API key: {e}", err=True)
        raise SystemExit(1)


@auth.command("show")
def auth_show():
    """Show current API key configuration."""
    key = get_api_key()
    if key:
        masked = key[:4] + "*" * (len(key) - 8) + key[-4:]
        click.echo(f"API key configured: {masked}")
    else:
        click.echo("No API key configured.")
        click.echo("\nTo configure:")
        click.echo("  1. Run: fireflies-auth store")
        click.echo("  2. Or set: export FIREFLIES_API_KEY=your_key")


@auth.command("delete")
def auth_delete():
    """Delete stored API key from system keychain."""
    try:
        delete_api_key()
        click.echo("API key deleted from system keychain.")
    except Exception as e:
        click.echo(f"Error deleting API key: {e}", err=True)


@click.group()
def sync_cli():
    """Sync and manage local Fireflies transcripts."""
    pass


@sync_cli.command("run")
@click.option("--days", "-d", default=5, help="Days to sync (default: 5)")
@click.option("--force", "-f", is_flag=True, help="Force re-sync existing transcripts")
@click.option("--quiet", "-q", is_flag=True, help="Suppress progress output")
def sync_run(days: int, force: bool, quiet: bool):
    """Sync transcripts from Fireflies to local storage."""
    from .sync import TranscriptSync
    import asyncio

    sync = TranscriptSync()

    def progress(current, total, transcript):
        if not quiet:
            title = transcript.title[:50] if transcript.title else "Untitled"
            click.echo(f"[{current}/{total}] {title}...")

    try:
        stats = asyncio.run(
            sync.sync(days=days, force=force, progress_callback=progress)
        )
        click.echo(f"\nSync complete:")
        click.echo(f"  Total found: {stats['total']}")
        click.echo(f"  Synced: {stats['synced']}")
        click.echo(f"  Skipped: {stats['skipped']}")
        if stats["errors"]:
            click.echo(f"  Errors: {stats['errors']}")
            for err in stats["error_details"]:
                click.echo(f"    - {err}", err=True)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)


@sync_cli.command("init")
@click.option(
    "--days", "-d",
    type=click.Choice(["15", "30", "90"]),
    default="30",
    help="Days of history to sync (default: 30)"
)
def sync_init(days: str):
    """Initialize local storage with historical transcripts."""
    from .sync import TranscriptSync
    import asyncio

    days_int = int(days)
    click.echo(f"Initializing with {days_int} days of history...")

    sync = TranscriptSync()

    def progress(current, total, transcript):
        title = transcript.title[:50] if transcript.title else "Untitled"
        click.echo(f"[{current}/{total}] {title}...")

    try:
        stats = asyncio.run(
            sync.sync(days=days_int, force=True, progress_callback=progress)
        )
        click.echo(f"\nInitialization complete:")
        click.echo(f"  Total found: {stats['total']}")
        click.echo(f"  Synced: {stats['synced']}")
        if stats["errors"]:
            click.echo(f"  Errors: {stats['errors']}")
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)


@sync_cli.command("list")
@click.option("--days", "-d", type=int, help="Filter to last N days")
@click.option("--limit", "-l", default=20, help="Maximum results (default: 20)")
def sync_list(days: int | None, limit: int):
    """List locally synced transcripts."""
    from .sync import TranscriptSync
    from datetime import datetime

    sync = TranscriptSync()
    transcripts = sync.list_transcripts(days=days, limit=limit)

    if not transcripts:
        click.echo("No transcripts found. Run 'fireflies-sync run' to sync.")
        return

    click.echo(f"Found {len(transcripts)} transcripts:\n")
    for t in transcripts:
        date_str = "Unknown"
        if t["date"]:
            date_str = datetime.fromtimestamp(t["date"] / 1000).strftime("%Y-%m-%d %H:%M")
        duration = f"{t['duration']:.0f}min" if t["duration"] else "?"

        click.echo(f"  {t['id'][:8]}  {date_str}  [{duration}]  {t['title'][:50]}")


@sync_cli.command("search")
@click.argument("query")
@click.option("--limit", "-l", default=20, help="Maximum results (default: 20)")
def sync_search(query: str, limit: int):
    """Search local transcripts by keyword."""
    from .sync import TranscriptSync
    from datetime import datetime

    sync = TranscriptSync()
    results = sync.search(query, limit=limit)

    if not results:
        click.echo(f"No transcripts found matching '{query}'.")
        return

    click.echo(f"Found {len(results)} transcripts matching '{query}':\n")
    for t in results:
        date_str = "Unknown"
        if t["date"]:
            date_str = datetime.fromtimestamp(t["date"] / 1000).strftime("%Y-%m-%d")
        click.echo(f"  {t['id'][:8]}  {date_str}  {t['title'][:50]}")


@sync_cli.command("stats")
def sync_stats():
    """Show sync statistics."""
    from .sync import TranscriptSync

    sync = TranscriptSync()
    stats = sync.stats()

    click.echo("Fireflies Local Sync Statistics:")
    click.echo(f"  Total transcripts: {stats['total_transcripts']}")
    click.echo(f"  Last sync: {stats['last_sync'] or 'Never'}")
    click.echo(f"  Storage: {stats['storage_dir']}")
    click.echo(f"  Storage size: {stats['storage_size_mb']} MB")


@sync_cli.command("read")
@click.argument("transcript_id")
@click.option(
    "--format", "-f",
    type=click.Choice(["txt", "srt", "json"]),
    default="txt",
    help="Output format (default: txt)"
)
def sync_read(transcript_id: str, format: str):
    """Read a transcript from local storage."""
    from .sync import TranscriptSync

    sync = TranscriptSync()

    # Allow partial ID match
    transcripts = sync.list_transcripts(limit=1000)
    full_id = None
    for t in transcripts:
        if t["id"].startswith(transcript_id):
            full_id = t["id"]
            break

    if not full_id:
        click.echo(f"Transcript not found: {transcript_id}", err=True)
        raise SystemExit(1)

    content = sync.get_transcript_content(full_id, format=format)
    if content:
        click.echo(content)
    else:
        click.echo(f"Content not found for format: {format}", err=True)
        raise SystemExit(1)


# Main entry points
def main_auth():
    """Entry point for fireflies-auth."""
    auth()


def main_sync():
    """Entry point for fireflies-sync."""
    sync_cli()
