"""Live meeting feed from the Fireflies Realtime API.

Pipeline:

    Fireflies Realtime (Socket.IO) -> ChunkMerger -> LiveWriter
        events.jsonl    raw events (replayable)
        transcript.txt  settled lines, append-only
        digest.log      one line per window (Monitor tails this)
        status.json     daemon state

The Realtime API (beta) emits `transcription.broadcast` events with
transcript_id, chunk_id, text, speaker_name, start_time, end_time. A repeated
chunk_id is a revision of that chunk; there is no "final" flag, so a chunk is
treated as settled once it has gone `settle_s` seconds without an update.

Observed 2026-11-10 (not in the docs): the server wraps every event in an
envelope {type, message, timestamp, payload}. parse_event accepts both shapes.

Every digest line that is not transcript content starts with "## " so a
Monitor filter can always see connect, disconnect, error and end states.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REALTIME_URL = "wss://api.fireflies.ai"
REALTIME_PATH = "/ws/realtime"
LIVE_ROOT = Path(os.environ.get("FF_LIVE_DIR", Path.home() / ".claude/personal/live"))

ACTIVE_MEETINGS_QUERY = """
query ActiveMeetings {
  active_meetings { id title organizer_email start_time state privacy }
}
"""

# Monitor cut notifications at about 400-500 characters in the 2026-11-10 run,
# so the default digest cap stays under that.
DEFAULT_MAX_CHARS = 400
TERMINAL_STATES = ("ended", "error", "stopped")


def describe_exc(e: BaseException) -> str:
    """Type, message and cause chain. httpx.ConnectError can have an empty str(),
    which is what made the 2026-11-10 end-check failures unreadable."""
    parts: list[str] = []
    seen: set[int] = set()
    cur: BaseException | None = e
    while cur is not None and id(cur) not in seen and len(parts) < 5:
        seen.add(id(cur))
        msg = str(cur)
        parts.append(f"{type(cur).__name__}: {msg}" if msg else f"{type(cur).__name__} {cur!r}")
        cur = cur.__cause__ or cur.__context__
    return " <- ".join(parts)


# --------------------------------------------------------------------------- model


@dataclass
class Chunk:
    chunk_id: str
    speaker: str
    text: str
    start: float
    end: float
    first_seen: float
    last_update: float
    revisions: int = 0
    settled: bool = False


def parse_event(raw: Any) -> dict | None:
    """Return the transcription fields from a raw event, or None if it is not one.

    Accepts the documented bare payload and the observed envelope
    {type, message, timestamp, payload}.
    """
    if not isinstance(raw, dict):
        return None
    body = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
    if "chunk_id" not in body or "text" not in body:
        return None
    return {
        "transcript_id": body.get("transcript_id"),
        "chunk_id": str(body["chunk_id"]),
        "text": str(body.get("text") or ""),
        "speaker": str(body.get("speaker_name") or "Unknown"),
        "start": float(body.get("start_time") or 0.0),
        "end": float(body.get("end_time") or 0.0),
    }


class ChunkMerger:
    """Merge revised chunks and decide when each one is settled."""

    def __init__(self, settle_s: float = 4.0, clock: Callable[[], float] = time.monotonic):
        self.settle_s = settle_s
        self.clock = clock
        self.chunks: dict[str, Chunk] = {}
        self.late_revisions = 0  # revisions that arrived after a chunk was settled

    def ingest(self, ev: dict) -> Chunk:
        now = self.clock()
        c = self.chunks.get(ev["chunk_id"])
        if c is None:
            c = Chunk(ev["chunk_id"], ev["speaker"], ev["text"], ev["start"], ev["end"], now, now)
            self.chunks[c.chunk_id] = c
            return c
        if c.settled:
            self.late_revisions += 1
        c.text, c.speaker, c.end = ev["text"], ev["speaker"], ev["end"]
        c.start = min(c.start, ev["start"]) if c.start else ev["start"]
        c.last_update = now
        c.revisions += 1
        return c

    def settle(self, force: bool = False) -> list[Chunk]:
        """Mark and return chunks that have been quiet for settle_s, in start order."""
        now = self.clock()
        ready = [
            c
            for c in self.chunks.values()
            if not c.settled and (force or now - c.last_update >= self.settle_s)
        ]
        for c in ready:
            c.settled = True
        return sorted(ready, key=lambda c: (c.start, c.first_seen))

    @property
    def pending(self) -> int:
        return sum(1 for c in self.chunks.values() if not c.settled)


def fmt_ts(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def load_watch_terms(path: str | Path | None) -> list[str]:
    if not path:
        return []
    terms = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            terms.append(line)
    return terms


def find_hits(text: str, terms: Iterable[str]) -> list[str]:
    """Return watch terms found in text (case-insensitive, word-bounded)."""
    hits = []
    for t in terms:
        if re.search(rf"(?<!\w){re.escape(t)}(?!\w)", text, re.IGNORECASE):
            hits.append(t)
    return hits


def build_digest(chunks: list[Chunk], terms: list[str], max_chars: int) -> str | None:
    """One line summarising a window of settled chunks; None if the window is empty.

    Consecutive chunks by the same speaker are joined. If the line exceeds
    max_chars it is cut and says how much was omitted; transcript.txt has it all.
    """
    if not chunks:
        return None
    turns: list[tuple[str, list[str]]] = []
    for c in chunks:
        text = c.text.strip()
        if not text:
            continue
        if turns and turns[-1][0] == c.speaker:
            turns[-1][1].append(text)
        else:
            turns.append((c.speaker, [text]))
    if not turns:
        return None
    body = " | ".join(f"{spk}: {' '.join(parts)}" for spk, parts in turns)
    hits = find_hits(body, terms)
    span = f"[{fmt_ts(chunks[0].start)}-{fmt_ts(max(c.end for c in chunks))}]"
    prefix = f"{span} " + (f"!! {', '.join(hits)} !! " if hits else "")
    if len(prefix) + len(body) > max_chars:
        suffix_len = len(f" …(+{len(body)} chars, see transcript)")  # longest possible suffix
        keep = max(0, max_chars - len(prefix) - suffix_len)
        cut = body[:keep].rstrip()
        body = cut + f" …(+{len(body) - len(cut)} chars, see transcript)"
    return prefix + body


# --------------------------------------------------------------------------- output


@dataclass
class LiveStatus:
    transcript_id: str
    title: str | None = None
    state: str = "starting"  # starting|connected|reconnecting|ended|error|stopped
    pid: int = field(default_factory=os.getpid)
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_event_at: str | None = None
    events: int = 0
    chunks: int = 0
    settled: int = 0
    late_revisions: int = 0
    digests: int = 0
    last_error: str | None = None
    session_dir: str = ""
    runs: int = 1  # feed processes that have written to this directory
    first_started_at: str | None = None


class LiveWriter:
    """Owns the per-meeting directory and every file in it."""

    def __init__(self, transcript_id: str, root: Path = LIVE_ROOT, title: str | None = None):
        self.dir = Path(root) / transcript_id
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        self.events_path = self.dir / "events.jsonl"
        self.transcript_path = self.dir / "transcript.txt"
        self.digest_path = self.dir / "digest.log"
        self.status_path = self.dir / "status.json"
        self.status = LiveStatus(transcript_id, title=title, session_dir=str(self.dir))
        self.status.first_started_at = self.status.started_at
        # A restart in the same meeting appends to events.jsonl and transcript.txt,
        # so the counts carry over too (before 2026-11-11 they reset to this run only).
        self.base_chunks = 0
        self.base_late_revisions = 0
        prior = self._prior_status()
        if prior:
            for k in ("events", "settled", "digests"):
                setattr(self.status, k, int(prior.get(k) or 0))
            self.base_chunks = int(prior.get("chunks") or 0)
            self.base_late_revisions = int(prior.get("late_revisions") or 0)
            self.status.chunks = self.base_chunks
            self.status.late_revisions = self.base_late_revisions
            self.status.runs = int(prior.get("runs") or 1) + 1
            self.status.first_started_at = prior.get("first_started_at") or prior.get("started_at")
            self.status.title = title or prior.get("title")
        current = Path(root) / "current"
        try:
            if current.is_symlink() or current.exists():
                current.unlink()
            current.symlink_to(self.dir.name)
        except OSError:
            pass
        self.write_status()

    def _prior_status(self) -> dict | None:
        try:
            st = json.loads(self.status_path.read_text())
        except (OSError, ValueError):
            return None
        return st if st.get("transcript_id") == self.status.transcript_id else None

    def _append(self, path: Path, line: str) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")
            f.flush()

    def raw(self, event_name: str, data: Any) -> None:
        rec = {"t": time.time(), "event": event_name, "data": data}
        self._append(self.events_path, json.dumps(rec, ensure_ascii=False))
        self.status.events += 1
        self.status.last_event_at = datetime.now(timezone.utc).isoformat()

    def transcript(self, chunks: list[Chunk]) -> None:
        for c in chunks:
            if c.text.strip():
                self._append(
                    self.transcript_path, f"[{fmt_ts(c.start)}] {c.speaker}: {c.text.strip()}"
                )
        self.status.settled += len(chunks)

    def digest(self, line: str) -> None:
        self._append(self.digest_path, line)
        if not line.startswith("## "):
            self.status.digests += 1

    def control(self, msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.digest(f"## {stamp} {msg}")

    def write_status(self) -> None:
        tmp = self.status_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self.status), indent=2))
        tmp.replace(self.status_path)


# --------------------------------------------------------------------------- engine


class LiveFeed:
    """Glue: raw events in, files out. Transport-agnostic so replay and tests share it."""

    def __init__(
        self,
        writer: LiveWriter,
        settle_s: float = 4.0,
        window_s: float = 60.0,
        terms: list[str] | None = None,
        max_chars: int = DEFAULT_MAX_CHARS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.writer = writer
        self.merger = ChunkMerger(settle_s, clock)
        self.window_s = window_s
        self.terms = terms or []
        self.max_chars = max_chars
        self.clock = clock
        self._window: list[Chunk] = []
        self._window_started = clock()

    def on_event(self, event_name: str, data: Any) -> None:
        self.writer.raw(event_name, data)
        ev = parse_event(data)
        if ev is not None:
            self.merger.ingest(ev)
            self.writer.status.chunks = self.writer.base_chunks + len(self.merger.chunks)

    def tick(self, force: bool = False) -> None:
        """Settle quiet chunks; flush the digest window when it is due (or forced)."""
        settled = self.merger.settle(force=force)
        if settled:
            self.writer.transcript(settled)
            self._window.extend(settled)
        due = self.clock() - self._window_started >= self.window_s
        if (due or force) and self._window:
            line = build_digest(self._window, self.terms, self.max_chars)
            if line:
                self.writer.digest(line)
            self._window = []
        if due or force:
            self._window_started = self.clock()
        self.writer.status.late_revisions = (
            self.writer.base_late_revisions + self.merger.late_revisions
        )
        self.writer.write_status()

    def finish(self, state: str, msg: str) -> None:
        self.tick(force=True)
        self.writer.status.state = state
        self.writer.control(msg)
        self.writer.write_status()


def _http_client(api_key: str):
    import httpx

    return httpx.AsyncClient(headers={"Authorization": f"Bearer {api_key}"}, timeout=30)


async def fetch_active_meetings(api_key: str, client: Any = None) -> list[dict]:
    """The active_meetings query. Pass `client` to reuse one connection pool (run_live does)."""
    if client is None:
        async with _http_client(api_key) as c:
            return await fetch_active_meetings(api_key, c)
    r = await client.post("https://api.fireflies.ai/graphql", json={"query": ACTIVE_MEETINGS_QUERY})
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise ValueError(f"GraphQL errors: {data['errors']}")
    return data.get("data", {}).get("active_meetings") or []


async def run_live(
    feed: LiveFeed,
    api_key: str,
    transcript_id: str,
    url: str = REALTIME_URL,
    path: str = REALTIME_PATH,
    tick_s: float = 1.0,
    end_check_s: float = 60.0,
    active_check: Callable[[], Any] | None = None,
    max_reconnect_s: float = 300.0,
) -> str:
    """Stream one meeting until it ends, auth fails, or reconnects run out.

    Returns the final state. `active_check` is an async callable returning the
    list of active meeting ids; it defaults to the active_meetings query.
    """
    import socketio

    w = feed.writer
    stop = asyncio.Event()
    final = {"state": "stopped", "msg": "stopped"}
    sio = socketio.AsyncClient(
        reconnection=True, reconnection_delay=2, reconnection_delay_max=30, logger=False
    )
    disconnected_since: list[float | None] = [None]

    def end(state: str, msg: str) -> None:
        final.update(state=state, msg=msg)
        stop.set()

    @sio.on("*")
    async def _any(event, *args):
        data = args[0] if args else None
        feed.on_event(event, data)
        if event == "auth.failed":
            msg = data.get("message") if isinstance(data, dict) else data
            w.status.last_error = f"auth.failed: {msg}"
            end("error", f"auth failed: {msg}")
        elif event == "connection.error":
            msg = data.get("message") if isinstance(data, dict) else data
            w.status.last_error = f"connection.error: {msg}"
            w.control(f"connection error: {msg}")
        elif event == "auth.success":
            w.control("authenticated")

    @sio.event
    async def connect():
        disconnected_since[0] = None
        w.status.state = "connected"
        w.control(f"connected to {transcript_id}")
        w.write_status()

    async def do_connect() -> None:
        await sio.connect(
            url,
            socketio_path=path,
            transports=["websocket"],
            auth={"token": f"Bearer {api_key}", "transcriptId": transcript_id},
            wait_timeout=20,
        )

    async def manual_reconnect() -> None:
        # python-socketio retries transport failures itself, but treats a
        # server-initiated disconnect as final. Retry those while the meeting
        # is live; the ticker's max_reconnect_s and end check bound the loop.
        delay = 2.0
        while not stop.is_set() and not sio.connected:
            await asyncio.sleep(delay)
            if stop.is_set():
                return
            try:
                await do_connect()
            except Exception as e:
                w.control(f"reconnect failed: {describe_exc(e)}")
                delay = min(delay * 2, 30.0)

    reconnect_task: list[asyncio.Task | None] = [None]

    @sio.event
    async def disconnect(*reason):
        if stop.is_set():
            return
        disconnected_since[0] = disconnected_since[0] or time.monotonic()
        w.status.state = "reconnecting"
        why = ", ".join(map(str, reason)) or "no reason"
        w.control(f"disconnected ({why}); reconnecting")
        w.write_status()
        if "server" in why and (reconnect_task[0] is None or reconnect_task[0].done()):
            reconnect_task[0] = asyncio.ensure_future(manual_reconnect())

    # One long-lived client for the end checks, instead of a new one per check
    # (the 2026-11-10 ConnectErrors came from the per-check client).
    http = None if active_check else _http_client(api_key)

    async def is_active() -> bool:
        if active_check:
            ids = await active_check()
        else:
            ids = [m["id"] for m in await fetch_active_meetings(api_key, http)]
        return transcript_id in ids

    async def ticker():
        last_end_check = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(tick_s)
            feed.tick()
            since = disconnected_since[0]
            if since and time.monotonic() - since > max_reconnect_s:
                end("error", f"gave up reconnecting after {int(max_reconnect_s)}s")
            if time.monotonic() - last_end_check >= end_check_s:
                last_end_check = time.monotonic()
                try:
                    if not await is_active():
                        end("ended", "meeting no longer active; feed ended")
                except Exception as e:  # a failed check must not kill the feed
                    w.status.last_error = f"active-meeting check: {describe_exc(e)}"
                    w.control(f"active-meeting check failed: {describe_exc(e)}")

    async def close_http() -> None:
        if http is not None:
            try:
                await http.aclose()
            except Exception:
                pass

    try:
        await do_connect()
    except Exception as e:
        w.status.last_error = describe_exc(e)
        feed.finish("error", f"could not connect: {describe_exc(e)}")
        try:
            await sio.shutdown()  # close the aiohttp session the failed attempt opened
        except Exception:
            pass
        await close_http()
        return "error"

    t = asyncio.create_task(ticker())
    try:
        await stop.wait()
    except asyncio.CancelledError:
        final.update(state="stopped", msg="stopped by user")
    finally:
        stop.set()  # so our own disconnect below is not reported as a drop
        t.cancel()
        if reconnect_task[0]:
            reconnect_task[0].cancel()
        if sio.connected:
            await sio.disconnect()
        await close_http()
        feed.finish(final["state"], final["msg"])
    return final["state"]


async def replay(
    feed: LiveFeed,
    events: list[dict],
    speed: float = 1.0,
    tick_s: float = 0.25,
) -> None:
    """Feed a recorded events.jsonl through the pipeline at `speed`x real time."""
    if not events:
        feed.finish("ended", "replay: no events")
        return
    feed.writer.status.state = "connected"
    feed.writer.control("replay started")
    t0 = events[0]["t"]
    start = time.monotonic()
    i = 0
    while i < len(events):
        due = (events[i]["t"] - t0) / speed
        now = time.monotonic() - start
        if now >= due:
            feed.on_event(events[i]["event"], events[i]["data"])
            i += 1
            continue
        await asyncio.sleep(min(tick_s, due - now))
        feed.tick()
    feed.finish("ended", "replay finished")


def read_events(path: str | Path) -> list[dict]:
    out = []
    for line in Path(path).read_text().splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


# --------------------------------------------------------------------------- readers (MCP tools)


_TS = re.compile(r"^\[(?:(\d+):)?(\d{2}):(\d{2})\]")


def _line_seconds(line: str) -> float | None:
    m = _TS.match(line)
    if not m:
        return None
    h, mm, ss = m.groups()
    return int(h or 0) * 3600 + int(mm) * 60 + int(ss)


def resolve_session(meeting: str | None = None, root: Path | None = None) -> Path | None:
    d = Path(root or LIVE_ROOT) / (meeting or "current")
    return d.resolve() if d.exists() else None


def read_status(meeting: str | None = None, root: Path | None = None) -> dict | None:
    d = resolve_session(meeting, root)
    if not d or not (d / "status.json").exists():
        return None
    st = json.loads((d / "status.json").read_text())
    pid = st.get("pid")
    try:
        os.kill(int(pid), 0)
        st["process_alive"] = True
    except (OSError, TypeError, ValueError):
        st["process_alive"] = False
    return st


def follow(
    meeting: str | None = None,
    root: Path | None = None,
    emit: Callable[[str], None] = lambda s: print(s, flush=True),
    poll_s: float = 0.5,
    from_end: bool = False,
    reset: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Print digest.log lines for a Monitor, resuming where the last follow stopped.

    Monitor expires after 30 minutes. The read position is saved in follow.offset
    after every line, so a re-armed follow neither misses nor repeats lines.
    It returns once the feed is finished and every line has been printed:
    0 when the feed reached a terminal state, 1 when its process vanished without one.
    """
    d = resolve_session(meeting, root)
    if d is None:
        emit(f"## follow: no live session for {meeting or 'current'}")
        return 1
    digest, off_path = d / "digest.log", d / "follow.offset"
    if reset and off_path.exists():
        off_path.unlink()
    offset = None
    if off_path.exists():
        try:
            offset = int(off_path.read_text().strip())
        except ValueError:
            offset = None
    if offset is None:
        offset = digest.stat().st_size if (from_end and digest.exists()) else 0
    while True:
        if digest.exists():
            if digest.stat().st_size < offset:  # file replaced; start over
                offset = 0
            with digest.open("rb") as f:
                f.seek(offset)
                while True:
                    raw = f.readline()
                    if not raw.endswith(b"\n"):
                        break  # nothing more, or a line still being written
                    emit(raw.decode("utf-8", "replace").rstrip("\n"))
                    offset = f.tell()
                    off_path.write_text(str(offset))
        st = read_status(meeting, root) or {}
        if st.get("state") in TERMINAL_STATES and not st.get("process_alive"):
            return 0
        if st and not st.get("process_alive"):
            emit(f"## follow: feed process is not running (last state: {st.get('state')}); stopping")
            return 1
        sleep(poll_s)


def transcript_tail(
    meeting: str | None = None,
    minutes: float | None = None,
    last: int | None = None,
    root: Path | None = None,
) -> list[str]:
    """Settled transcript lines: the last `minutes` of meeting time, or the last `last` lines."""
    d = resolve_session(meeting, root)
    p = d / "transcript.txt" if d else None
    if not p or not p.exists():
        return []
    lines = p.read_text().splitlines()
    if minutes is not None and lines:
        end = max((s for s in map(_line_seconds, lines) if s is not None), default=0)
        cutoff = end - minutes * 60
        lines = [ln for ln in lines if (_line_seconds(ln) or 0) >= cutoff]
    if last:
        lines = lines[-last:]
    return lines
