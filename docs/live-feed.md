# Live meeting feed

Feed a meeting in progress into a Claude Code session, so John can respond during the call instead of after it.

## Flow

```
Fireflies Realtime API (Socket.IO, beta)
        │ transcription.broadcast (revisions share a chunk_id)
        ▼
ff-live run  ── ChunkMerger: a chunk is final after `settle` s with no revision
        │      LiveFeed:   settled chunks → transcript.txt; every `window` s → one digest line
        ▼
~/.claude/personal/live/<id>/{digest.log, transcript.txt, status.json, events.jsonl}
        │
        ├─ Monitor (tail -F digest.log) in a listener session ─► SendMessage ─► working session
        ├─ Monitor in the working session itself (direct mode)
        └─ MCP pull: live_status, live_transcript, live_active_meetings
```

Why not push through MCP: MCP tools answer calls; they don't start turns. Monitor turns stdout lines into session events, and SendMessage moves a message between local sessions, so those two do the pushing. The listener session absorbs the stream, so the working session's context only receives what matters. The relay rules are in the `meeting-listen` skill (`_docs/claude-code/workspace/skills/meeting-listen/SKILL.md`).

## Model

| Piece | Decision | Why |
|---|---|---|
| Chunk finality | Final after `settle` (4s) without a revision | The API has no final flag; a repeated `chunk_id` is the only revision signal |
| Digest | One line per `window` (60s); same-speaker turns joined; capped at `max_chars` (700) with an explicit "(+N chars, see transcript)" | Every Monitor line is a session event; bounded volume keeps the listener cheap |
| Watch terms | Word-bounded, case-insensitive; hits shown as `!! a, b !!` | Gives the listener a cheap salience cue; relevance judgment stays with Claude |
| Control lines | `## HH:MM:SS msg` for connect, auth, drops, errors, end | Silence must never look like health; every terminal state emits a line |
| End detection | `active_meetings` every 60s; gone means ended | The socket alone doesn't say whether the meeting is over |
| Reconnect | python-socketio for transport errors, plus our own loop for server-initiated disconnects; give up after 300s down | python-socketio treats a server disconnect as final (found by test) |
| Meeting choice | `--meeting` required; never auto-picks | PHI gate: John confirms each meeting carries no PHI (Team plan, no BAA) |
| Storage | `~/.claude/personal/live/`, 0700, local only | Same tier as the existing transcript cache |

## Verified

Against the real endpoint, 2026-11-10:

- `active_meetings` GraphQL works with our key; no meetings were active at the time.
- The Socket.IO handshake to `wss://api.fireflies.ai` with path `/ws/realtime` connects.
- With a fake id, or with a bad key, the server returns `auth.failed: Invalid authentication token`. The two cases look the same.
- With a real meeting that has already finished, there is no `auth.failed`; instead the server returns `connection.error: Unable to connect. Please try again later`, then disconnects. So the key is accepted, and the meeting has to be live.
- Events are wrapped in `{type, message, timestamp, payload}`, which the docs don't show.

Against the fake server (`tests/fake_fireflies.py`, same envelope), 18 tests in `tests/test_live.py`:

- revisions are merged so each chunk lands once, final text only
- digest lines, watch hits and truncation
- auth failure, unreachable server, server drop with reconnect and resume, and user stop are each reported correctly
- the meeting-ended path
- replay reproduces the live transcript byte for byte
- the MCP readers return the right lines

End-to-end in Claude Code: `ff-live run` against the fake server, with Monitor tailing `digest.log`. Four digest events arrived in-session, with the hits marked, and the stop line was delivered.

## Not verified

The first real meeting answers most of these.

- **Plan entitlement.** Fireflies doesn't document which plans include Realtime. The handshake suggests our key is accepted, but no live event has been received yet.
- **Bot requirement.** Whether events flow only when the Fireflies bot is in the meeting is undocumented, though transcription itself needs the bot.
- **Latency, revision behaviour, timestamps.** We don't know the real latency, how often chunks are revised, whether a revision can arrive after `settle`, or whether `start_time` is relative to the meeting start. `late_revisions` in `status.json` counts revisions that arrive after settle, so the first meeting will show whether 4s is enough.
- **Joining mid-meeting.** Whether connecting partway through delivers earlier speech is unknown.
- **The SendMessage relay between two top-level sessions** was not exercised; doing so would have meant messaging a real session unasked. The Monitor leg was exercised.

## First live run (checklist)

1. Pick a meeting with no PHI and confirm that with John. The Fireflies bot must be in it.
2. `ff-live meetings`, then `ff-live run -m <id> --window 30`, then Monitor on `digest.log`.
3. Afterwards, check `status.json`: `late_revisions`, `events`/`chunks`, and `last_error`. Compare `transcript.txt` with the Fireflies transcript once it syncs.
4. Keep `events.jsonl` (no PHI) as a real fixture for `ff-live replay` and tests, then update this document's Not verified list.
