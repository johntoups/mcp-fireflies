"""Fake Fireflies Realtime server (Socket.IO on aiohttp) for tests and demos.

Mirrors what the real endpoint did on 2026-11-10: events wrapped in
{type, message, timestamp, payload}; auth.failed then disconnect on a bad token.
Script content is synthetic (no customer or patient data).

Demo:  python tests/fake_fireflies.py --port 8765 --pace 3
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

import socketio
from aiohttp import web

GOOD_TOKEN = "Bearer test-key"
TID = "01FAKEMEETING0000000000000"

# (speaker, final text, start, end). Each line is sent as 2-3 growing revisions.
SCRIPT = [
    ("Varada", "Thanks for joining. Let's walk the revised architecture diagram.", 0.0, 4.2),
    ("John", "Sure. The left side is unchanged since the August submission.", 4.5, 8.0),
    ("Varada", "Where does DataPower sit in the inbound path?", 8.3, 11.0),
    ("John", "We haven't confirmed that; it is one of the three open items.", 11.2, 15.1),
    ("Hannah", "Can you send the regional senders list by Friday?", 15.5, 18.9),
    ("John", "Yes, AdvantX, Epic and the standalone Meditech sites.", 19.0, 23.4),
    ("Varada", "And confirm backups are fifteen days with Multi-AZ.", 23.8, 27.0),
    ("John", "Confirmed, fifteen-day retention, Multi-AZ, no global database.", 27.2, 31.5),
]


def envelope(kind: str, payload: dict, message: str = "") -> dict:
    return {
        "type": kind,
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }


def revisions(text: str) -> list[str]:
    words = text.split()
    cuts = sorted({max(1, len(words) // 3), max(1, 2 * len(words) // 3), len(words)})
    return [" ".join(words[:c]) for c in cuts]


def make_app(pace: float = 0.05, drop_after: int | None = None, script=SCRIPT):
    """pace: seconds between revisions. drop_after: force-disconnect once after N lines."""
    sio = socketio.AsyncServer(async_mode="aiohttp", cors_allowed_origins="*")
    app = web.Application()
    sio.attach(app, socketio_path="/ws/realtime")
    state = {"connects": 0, "dropped": False, "sent_lines": 0}
    app["state"] = state

    async def stream(sid):
        await sio.emit("auth.success", envelope("auth.success", {"transcript_id": TID}), to=sid)
        await sio.emit(
            "connection.established",
            envelope("connection.established", {"transcript_id": TID}),
            to=sid,
        )
        for i, (spk, text, start, end) in enumerate(script):
            if i < state["sent_lines"]:
                continue  # resume after a reconnect
            revs = revisions(text)
            for j, partial in enumerate(revs):
                frac = (j + 1) / len(revs)
                await sio.emit(
                    "transcription.broadcast",
                    envelope(
                        "transcription.broadcast",
                        {
                            "transcript_id": TID,
                            "chunk_id": f"chunk_{i:03d}",
                            "text": partial,
                            "speaker_name": spk,
                            "start_time": start,
                            "end_time": start + (end - start) * frac,
                        },
                    ),
                    to=sid,
                )
                await asyncio.sleep(pace)
            state["sent_lines"] = i + 1
            if (
                drop_after is not None
                and not state["dropped"]
                and state["sent_lines"] == drop_after
            ):
                state["dropped"] = True
                await sio.disconnect(sid)
                return

    @sio.event
    async def connect(sid, environ, auth):
        state["connects"] += 1
        auth = auth or {}
        if auth.get("token") != GOOD_TOKEN or auth.get("transcriptId") != TID:

            async def reject():
                await asyncio.sleep(0.05)
                await sio.emit(
                    "auth.failed",
                    envelope(
                        "auth.failed",
                        {"transcript_id": auth.get("transcriptId")},
                        "Invalid authentication token",
                    ),
                    to=sid,
                )
                await sio.disconnect(sid)

            asyncio.ensure_future(reject())
            return True
        asyncio.ensure_future(stream(sid))
        return True

    return app


async def start(app, port: int = 0):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    actual = site._server.sockets[0].getsockname()[1]
    return runner, actual


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--pace", type=float, default=1.0)
    a = ap.parse_args()
    web.run_app(make_app(pace=a.pace), host="127.0.0.1", port=a.port)
