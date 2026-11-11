import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import fake_fireflies as ff  # noqa: E402

from mcp_fireflies import live  # noqa: E402
from mcp_fireflies.live import (  # noqa: E402
    Chunk,
    ChunkMerger,
    LiveFeed,
    LiveWriter,
    build_digest,
    find_hits,
    parse_event,
)


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def ev(cid, text, spk="A", start=0.0, end=1.0, wrapped=True):
    body = {
        "transcript_id": "T",
        "chunk_id": cid,
        "text": text,
        "speaker_name": spk,
        "start_time": start,
        "end_time": end,
    }
    return {"type": "transcription.broadcast", "payload": body} if wrapped else body


# ---------------------------------------------------------------- parse / merge


def test_parse_event_accepts_envelope_and_bare():
    assert parse_event(ev("c1", "hi"))["chunk_id"] == "c1"
    assert parse_event(ev("c1", "hi", wrapped=False))["speaker"] == "A"


def test_parse_event_ignores_non_transcription():
    assert parse_event({"type": "auth.success", "payload": {"transcript_id": "T"}}) is None
    assert parse_event("text") is None
    assert parse_event(None) is None


def test_merger_revision_replaces_text_and_settles_after_quiet():
    clk = FakeClock()
    m = ChunkMerger(settle_s=4, clock=clk)
    m.ingest(parse_event(ev("c1", "Where does")))
    clk.t = 1
    m.ingest(parse_event(ev("c1", "Where does DataPower sit?", end=3)))
    clk.t = 4.5
    assert m.settle() == []  # only 3.5s since last revision
    clk.t = 5.1
    out = m.settle()
    assert [c.text for c in out] == ["Where does DataPower sit?"]
    assert out[0].revisions == 1 and m.pending == 0


def test_merger_counts_late_revision():
    clk = FakeClock()
    m = ChunkMerger(settle_s=1, clock=clk)
    m.ingest(parse_event(ev("c1", "a")))
    clk.t = 2
    m.settle()
    m.ingest(parse_event(ev("c1", "a b")))
    assert m.late_revisions == 1


def test_settle_orders_by_start_time():
    clk = FakeClock()
    m = ChunkMerger(settle_s=0, clock=clk)
    m.ingest(parse_event(ev("c2", "second", start=5)))
    m.ingest(parse_event(ev("c1", "first", start=1)))
    assert [c.text for c in m.settle()] == ["first", "second"]


# ---------------------------------------------------------------- digest


def _c(spk, text, start, end=None):
    return Chunk("x", spk, text, start, end or start + 1, 0, 0, settled=True)


def test_digest_joins_same_speaker_and_marks_hits():
    line = build_digest(
        [_c("V", "Where is", 8), _c("V", "DataPower?", 9), _c("J", "Unknown.", 11, 15)],
        ["datapower"],
        700,
    )
    assert line == "[00:08-00:15] !! datapower !! V: Where is DataPower? | J: Unknown."


def test_digest_truncates_and_says_so():
    line = build_digest([_c("A", "word " * 300, 0)], [], 200)
    assert len(line) <= 200 and "see transcript" in line


def test_digest_empty_window_is_none():
    assert build_digest([], [], 100) is None
    assert build_digest([_c("A", "   ", 0)], [], 100) is None


def test_find_hits_is_word_bounded():
    assert find_hits("the RDS cluster", ["RDS"]) == ["RDS"]
    assert find_hits("wordsmith", ["words"]) == []
    assert find_hits("Multi-AZ backups", ["Multi-AZ"]) == ["Multi-AZ"]


# ---------------------------------------------------------------- feed + files


def test_feed_writes_transcript_digest_and_status(tmp_path):
    clk = FakeClock()
    w = LiveWriter("T1", root=tmp_path, title="t")
    feed = LiveFeed(w, settle_s=2, window_s=10, terms=["Friday"], clock=clk)
    feed.on_event("transcription.broadcast", ev("c1", "Send it by", "H", 0, 1))
    clk.t = 1
    feed.on_event("transcription.broadcast", ev("c1", "Send it by Friday", "H", 0, 2))
    clk.t = 4
    feed.tick()  # settled, window not due yet
    assert (tmp_path / "T1/transcript.txt").read_text() == "[00:00] H: Send it by Friday\n"
    assert not (tmp_path / "T1/digest.log").exists()
    clk.t = 11
    feed.tick()
    assert (
        tmp_path / "T1/digest.log"
    ).read_text() == "[00:00-00:02] !! Friday !! H: Send it by Friday\n"
    st = json.loads((tmp_path / "T1/status.json").read_text())
    assert st["events"] == 2 and st["chunks"] == 1 and st["settled"] == 1 and st["digests"] == 1
    assert (tmp_path / "current").resolve() == (tmp_path / "T1").resolve()
    assert oct((tmp_path / "T1").stat().st_mode)[-3:] == "700"


def test_finish_flushes_pending_and_writes_control_line(tmp_path):
    clk = FakeClock()
    feed = LiveFeed(LiveWriter("T2", root=tmp_path), settle_s=99, window_s=99, clock=clk)
    feed.on_event("transcription.broadcast", ev("c1", "last words"))
    feed.finish("ended", "meeting no longer active; feed ended")
    lines = (tmp_path / "T2/digest.log").read_text().splitlines()
    assert lines[0].endswith("A: last words")
    assert lines[1].startswith("## ") and lines[1].endswith("feed ended")


def test_transcript_tail_minutes_and_last(tmp_path):
    w = LiveWriter("T3", root=tmp_path)
    w.transcript([_c("A", "one", 0), _c("B", "two", 400), _c("A", "three", 590)])
    assert live.transcript_tail("T3", minutes=5, root=tmp_path) == [
        "[06:40] B: two",
        "[09:50] A: three",
    ]
    assert live.transcript_tail(None, last=1, root=tmp_path) == ["[09:50] A: three"]
    assert live.transcript_tail("nope", root=tmp_path) == []


# ---------------------------------------------------------------- socket.io integration


async def _serve(**kw):
    app = ff.make_app(**kw)
    runner, port = await ff.start(app)
    return app, runner, f"http://127.0.0.1:{port}"


def _ids_until(active_for_s):
    loop = asyncio.get_event_loop()
    t_end = loop.time() + active_for_s

    async def check():
        return [ff.TID] if loop.time() < t_end else []

    return check


async def test_run_live_streams_until_meeting_ends(tmp_path):
    app, runner, url = await _serve(pace=0.01)
    try:
        w = LiveWriter(ff.TID, root=tmp_path)
        feed = LiveFeed(w, settle_s=0.1, window_s=0.3, terms=["DataPower", "Friday"])
        state = await asyncio.wait_for(
            live.run_live(
                feed,
                "test-key",
                ff.TID,
                url=url,
                tick_s=0.05,
                end_check_s=0.2,
                active_check=_ids_until(1.5),
            ),
            15,
        )
    finally:
        await runner.cleanup()
    assert state == "ended"
    transcript = (w.dir / "transcript.txt").read_text().splitlines()
    assert len(transcript) == len(ff.SCRIPT)  # each chunk once, final revision only
    assert transcript[2] == "[00:08] Varada: Where does DataPower sit in the inbound path?"
    digest = (w.dir / "digest.log").read_text()
    hit_lines = [ln for ln in digest.splitlines() if "!! " in ln and not ln.startswith("## ")]
    assert any("DataPower" in ln.split("!!")[1] for ln in hit_lines)
    assert any("Friday" in ln.split("!!")[1] for ln in hit_lines)
    assert "## " in digest and digest.rstrip().endswith("feed ended")
    st = json.loads((w.dir / "status.json").read_text())
    assert st["state"] == "ended" and st["late_revisions"] == 0


async def test_run_live_reports_auth_failure(tmp_path):
    app, runner, url = await _serve()
    try:
        w = LiveWriter(ff.TID, root=tmp_path)
        state = await asyncio.wait_for(
            live.run_live(
                LiveFeed(w), "wrong-key", ff.TID, url=url, tick_s=0.05, active_check=_ids_until(60)
            ),
            10,
        )
    finally:
        await runner.cleanup()
    assert state == "error"
    assert "auth failed: Invalid authentication token" in (w.dir / "digest.log").read_text()
    assert json.loads((w.dir / "status.json").read_text())["last_error"].startswith("auth.failed")


async def test_run_live_reconnects_after_server_drop(tmp_path):
    app, runner, url = await _serve(pace=0.01, drop_after=3)
    try:
        w = LiveWriter(ff.TID, root=tmp_path)
        feed = LiveFeed(w, settle_s=0.1, window_s=0.3)
        state = await asyncio.wait_for(
            live.run_live(
                feed,
                "test-key",
                ff.TID,
                url=url,
                tick_s=0.05,
                end_check_s=0.5,
                active_check=_ids_until(6),
            ),
            20,
        )
    finally:
        await runner.cleanup()
    assert state == "ended"
    assert app["state"]["connects"] >= 2
    digest = (w.dir / "digest.log").read_text()
    assert "reconnecting" in digest
    assert len((w.dir / "transcript.txt").read_text().splitlines()) == len(ff.SCRIPT)


async def test_run_live_unreachable_server(tmp_path):
    w = LiveWriter(ff.TID, root=tmp_path)
    state = await asyncio.wait_for(
        live.run_live(
            LiveFeed(w), "k", ff.TID, url="http://127.0.0.1:9", active_check=_ids_until(60)
        ),
        30,
    )
    assert state == "error"
    assert "could not connect" in (w.dir / "digest.log").read_text()


async def test_replay_reproduces_live_output(tmp_path):
    app, runner, url = await _serve(pace=0.01)
    try:
        w = LiveWriter(ff.TID, root=tmp_path / "a")
        await asyncio.wait_for(
            live.run_live(
                LiveFeed(w, settle_s=0.1, window_s=0.3),
                "test-key",
                ff.TID,
                url=url,
                tick_s=0.05,
                end_check_s=0.2,
                active_check=_ids_until(1.5),
            ),
            15,
        )
    finally:
        await runner.cleanup()
    events = live.read_events(w.events_path)
    w2 = LiveWriter("R", root=tmp_path / "b")
    await live.replay(LiveFeed(w2, settle_s=0.1, window_s=0.3), events, speed=4)
    assert (w2.dir / "transcript.txt").read_text() == (w.dir / "transcript.txt").read_text()


async def test_user_stop_is_not_reported_as_a_drop(tmp_path):
    app, runner, url = await _serve(pace=0.05)
    try:
        w = LiveWriter(ff.TID, root=tmp_path)
        task = asyncio.ensure_future(
            live.run_live(
                LiveFeed(w, settle_s=0.1, window_s=0.3),
                "test-key",
                ff.TID,
                url=url,
                tick_s=0.05,
                active_check=_ids_until(60),
            )
        )
        await asyncio.sleep(0.8)
        task.cancel()
        state = await asyncio.wait_for(task, 10)
    finally:
        await runner.cleanup()
    assert state == "stopped"
    digest = (w.dir / "digest.log").read_text()
    assert "reconnecting" not in digest
    assert digest.rstrip().endswith("stopped by user")


# ---------------------------------------------------------------- 2026-11-11 fixes


def test_describe_exc_shows_empty_message_and_cause_chain():
    class ConnectError(Exception):
        pass

    try:
        try:
            raise OSError("connection reset by peer")
        except OSError as inner:
            raise ConnectError("") from inner
    except ConnectError as e:
        text = live.describe_exc(e)
    assert text.startswith("ConnectError ConnectError(")
    assert text.endswith("<- OSError: connection reset by peer")


def test_restart_in_same_meeting_keeps_counts(tmp_path):
    clk = FakeClock()
    feed = LiveFeed(LiveWriter("T9", root=tmp_path), settle_s=1, window_s=1, clock=clk)
    feed.on_event("transcription.broadcast", ev("c1", "first run"))
    feed.finish("stopped", "stopped by user")
    w2 = LiveWriter("T9", root=tmp_path)
    feed2 = LiveFeed(w2, settle_s=1, window_s=1, clock=clk)
    feed2.on_event("transcription.broadcast", ev("c2", "second run"))
    feed2.finish("stopped", "stopped by user")
    st = json.loads((tmp_path / "T9/status.json").read_text())
    assert st["events"] == 2 and st["chunks"] == 2 and st["settled"] == 2 and st["digests"] == 2
    assert st["runs"] == 2 and st["first_started_at"] <= st["started_at"]
    # a different meeting starts from zero
    assert LiveWriter("T10", root=tmp_path).status.runs == 1


def _status(d, state, pid):
    (d / "status.json").write_text(json.dumps({"transcript_id": d.name, "state": state, "pid": pid}))


def test_follow_resumes_without_repeats_and_exits_when_feed_ends(tmp_path):
    import os

    d = tmp_path / "M1"
    d.mkdir()
    (d / "digest.log").write_text("## 10:00:00 connected to M1\nline one\n")
    _status(d, "connected", os.getpid())
    got, calls = [], []

    def sleep(_):
        calls.append(1)
        if len(calls) == 1:  # the feed writes a line, a partial line, then ends
            with (d / "digest.log").open("a") as f:
                f.write("line two\npart")
        elif len(calls) == 2:
            with (d / "digest.log").open("a") as f:
                f.write("ial\n## 10:05:00 meeting no longer active; feed ended\n")
            _status(d, "ended", None)

    rc = live.follow("M1", root=tmp_path, emit=got.append, sleep=sleep)
    assert rc == 0
    assert got == [
        "## 10:00:00 connected to M1",
        "line one",
        "line two",
        "partial",
        "## 10:05:00 meeting no longer active; feed ended",
    ]
    # a re-armed follow prints nothing it already printed
    again = []
    assert live.follow("M1", root=tmp_path, emit=again.append, sleep=lambda _: None) == 0
    assert again == []
    # --reset starts over
    again = []
    live.follow("M1", root=tmp_path, emit=again.append, reset=True, sleep=lambda _: None)
    assert again == got


def test_follow_reports_a_feed_that_died_without_a_final_state(tmp_path):
    d = tmp_path / "M2"
    d.mkdir()
    (d / "digest.log").write_text("line one\n")
    _status(d, "connected", None)
    got = []
    assert live.follow("M2", root=tmp_path, emit=got.append, sleep=lambda _: None) == 1
    assert got[0] == "line one" and got[-1].startswith("## follow: feed process is not running")


async def test_failed_end_check_logs_the_cause(tmp_path):
    app, runner, url = await _serve(pace=0.01)
    calls = []

    async def check():
        calls.append(1)
        if len(calls) == 1:
            try:
                raise OSError("dns lookup failed")
            except OSError as e:
                raise RuntimeError("") from e
        return []

    try:
        w = LiveWriter(ff.TID, root=tmp_path)
        state = await asyncio.wait_for(
            live.run_live(
                LiveFeed(w, settle_s=0.1, window_s=0.3),
                "test-key",
                ff.TID,
                url=url,
                tick_s=0.05,
                end_check_s=0.2,
                active_check=check,
            ),
            15,
        )
    finally:
        await runner.cleanup()
    assert state == "ended"
    digest = (w.dir / "digest.log").read_text()
    assert "active-meeting check failed: RuntimeError" in digest
    assert "<- OSError: dns lookup failed" in digest
