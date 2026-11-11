"""ff-live: stream a live Fireflies meeting into local files a Claude session can follow."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

import click

from .auth import get_api_key
from .live import (
    DEFAULT_MAX_CHARS,
    LIVE_ROOT,
    REALTIME_URL,
    LiveFeed,
    LiveWriter,
    fetch_active_meetings,
    follow,
    load_watch_terms,
    read_events,
    replay,
    run_live,
)


def _key() -> str:
    key = get_api_key()
    if not key:
        click.echo("No Fireflies API key. Run: fireflies-auth store", err=True)
        raise SystemExit(1)
    return key


def _session_dir(meeting: str | None) -> Path:
    d = LIVE_ROOT / (meeting or "current")
    if not d.exists():
        click.echo(f"No live session at {d}", err=True)
        raise SystemExit(1)
    return d.resolve()


async def _run_with_signals(coro):
    task = asyncio.ensure_future(coro)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
    try:
        return await task
    except asyncio.CancelledError:
        return "stopped"


@click.group()
def live():
    """Follow a live Fireflies meeting.

    Files land in ~/.claude/personal/live/<transcript_id>/ (override with FF_LIVE_DIR);
    ~/.claude/personal/live/current points at the latest session.
    """


@live.command("meetings")
def meetings():
    """List meetings Fireflies is transcribing right now."""
    ms = asyncio.run(fetch_active_meetings(_key()))
    if not ms:
        click.echo("No active meetings.")
        return
    for m in ms:
        state, start = m.get("state") or "?", m.get("start_time") or ""
        click.echo(f"{m['id']}  {state:7}  {start:25}  {m.get('title') or ''}")


@live.command("run")
@click.option(
    "--meeting",
    "-m",
    required=True,
    help="transcript id from `ff-live meetings` (no default, by design)",
)
@click.option("--window", default=60.0, show_default=True, help="Seconds per digest line")
@click.option(
    "--settle",
    default=4.0,
    show_default=True,
    help="Seconds without revision before a chunk counts as final",
)
@click.option(
    "--watch", "watch_file", type=click.Path(exists=True), help="File of watch terms, one per line"
)
@click.option(
    "--max-chars",
    default=DEFAULT_MAX_CHARS,
    show_default=True,
    help="Cap on one digest line (Monitor cuts notifications at about 400-500)",
)
@click.option(
    "--end-check", default=60.0, show_default=True, help="Seconds between active-meeting checks"
)
@click.option("--url", default=REALTIME_URL, hidden=True, help="Realtime endpoint (tests/demo)")
@click.option(
    "--skip-active-check", is_flag=True, hidden=True, help="Tests/demo only: never query the API"
)
def run(
    meeting: str,
    window: float,
    settle: float,
    watch_file: str | None,
    max_chars: int,
    end_check: float,
    url: str,
    skip_active_check: bool,
):
    """Stream one meeting until it ends. Requires an explicit meeting id."""
    key = _key()
    if skip_active_check:
        match = {"id": meeting, "title": "(active check skipped)"}

        async def always_active():
            return [meeting]

        active_check = always_active
    else:
        active = asyncio.run(fetch_active_meetings(key))
        match = next((m for m in active if m["id"] == meeting), None)
        active_check = None
    if not match:
        click.echo(f"{meeting} is not an active meeting. `ff-live meetings` lists them.", err=True)
        raise SystemExit(2)
    writer = LiveWriter(meeting, title=match.get("title"))
    feed = LiveFeed(
        writer,
        settle_s=settle,
        window_s=window,
        terms=load_watch_terms(watch_file),
        max_chars=max_chars,
    )
    click.echo(f"session dir: {writer.dir}")
    click.echo(f"digest:      {writer.digest_path}")
    state = asyncio.run(
        _run_with_signals(
            run_live(feed, key, meeting, url=url, end_check_s=end_check, active_check=active_check)
        )
    )
    if state == "stopped" and writer.status.state not in ("ended", "error", "stopped"):
        feed.finish("stopped", "stopped by user")
    click.echo(f"final state: {writer.status.state}")
    raise SystemExit(0 if writer.status.state in ("ended", "stopped") else 1)


@live.command("replay")
@click.argument("events_file", type=click.Path(exists=True))
@click.option("--speed", default=1.0, show_default=True, help="Replay speed multiplier")
@click.option(
    "--as-id", default=None, help="Session id to write under (default: replay-<timestamp>)"
)
@click.option("--window", default=60.0, show_default=True, help="Digest window, in meeting seconds")
@click.option("--settle", default=4.0, show_default=True, help="Settle time, in meeting seconds")
@click.option("--watch", "watch_file", type=click.Path(exists=True))
@click.option("--max-chars", default=DEFAULT_MAX_CHARS, show_default=True)
def replay_cmd(events_file, speed, as_id, window, settle, watch_file, max_chars):
    """Replay a recorded events.jsonl through the same pipeline (for tests and demos)."""
    sid = as_id or f"replay-{int(time.time())}"
    writer = LiveWriter(sid, title=f"replay of {Path(events_file).name}")
    t0 = time.monotonic()
    clock = lambda: (time.monotonic() - t0) * speed  # noqa: E731  meeting-time clock
    feed = LiveFeed(
        writer,
        settle_s=settle,
        window_s=window,
        terms=load_watch_terms(watch_file),
        max_chars=max_chars,
        clock=clock,
    )
    click.echo(f"session dir: {writer.dir}")
    asyncio.run(_run_with_signals(replay(feed, read_events(events_file), speed=speed)))
    if writer.status.state not in ("ended",):
        feed.finish("stopped", "replay stopped")


@live.command("status")
@click.option("--meeting", "-m", default=None)
def status(meeting):
    """Show the daemon status for a session (default: current)."""
    d = _session_dir(meeting)
    st = json.loads((d / "status.json").read_text())
    alive = _pid_alive(st.get("pid"))
    st["process_alive"] = alive
    click.echo(json.dumps(st, indent=2))


@live.command("stop")
@click.option("--meeting", "-m", default=None)
def stop(meeting):
    """Stop the daemon for a session (default: current)."""
    d = _session_dir(meeting)
    st = json.loads((d / "status.json").read_text())
    pid = st.get("pid")
    if not _pid_alive(pid):
        click.echo("Not running.")
        return
    os.kill(pid, signal.SIGTERM)
    click.echo(f"Sent SIGTERM to {pid}.")


@live.command("follow")
@click.option("--meeting", "-m", default=None)
@click.option("--from-end", is_flag=True, help="On the first follow, skip lines already written")
@click.option("--reset", is_flag=True, help="Forget the saved position and start again")
def follow_cmd(meeting, from_end, reset):
    """Print digest lines for a Monitor; a re-armed follow resumes where the last one stopped.

    Exits by itself once the feed has finished and every line has been printed.
    """
    try:
        rc = follow(meeting, from_end=from_end, reset=reset)
    except KeyboardInterrupt:
        rc = 0
    raise SystemExit(rc)


@live.command("transcript")
@click.option("--meeting", "-m", default=None)
@click.option("--last", "-n", default=40, show_default=True, help="Lines from the end (0 = all)")
def transcript(meeting, last):
    """Print settled transcript lines."""
    p = _session_dir(meeting) / "transcript.txt"
    lines = p.read_text().splitlines() if p.exists() else []
    for line in lines[-last:] if last else lines:
        click.echo(line)


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def main_live():
    live()


if __name__ == "__main__":
    sys.exit(main_live())
