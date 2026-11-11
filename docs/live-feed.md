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

## First live run (2026-11-10, HCA / Medaxion ARB Prep, about 61 min)

**Confirmed by the run:**
- Our Fireflies plan includes Realtime. Events flowed as soon as speech started, with the bot in the meeting.
- 3,004 events merged into 784 chunks; all 784 settled; 117 digest lines at a 30s window. Only one revision arrived after the 4s settle, so 4s is enough.
- Speaker names came from Fireflies diarisation and were reliable for named attendees.
- One real transport drop at 13:18:58. The client reconnected in 3s, **but about 40s of meeting time (18:18-19:00) never arrived**. The server does not replay speech missed during a disconnect. The Fireflies post-meeting transcript is the complete record.
- The SendMessage relay from the listener to a second top-level session (`hca-expanse-arb`) worked: 40+ relays plus the recap were delivered.
- The listener caught several conflicts live (incoming-traffic claim, multi-region vs no Aurora Global, RAI scope), and pulled New Relic data mid-call on request.

**Problems found:**
- **Monitor truncates long notifications.** This is independent of `--max-chars`, and lines were cut at about 400-500 characters. Read `ff-live transcript -n 5` whenever a line ends in `...(truncated)`. A future fix: lower `--max-chars` to about 400, or split a window into one line per speaker turn.
- **The active-meeting check failed** twice near the end with `ConnectError:` and no message. At the same moment a plain curl to the GraphQL endpoint answered, and the socket kept delivering. Cause not identified. Effect: the feed didn't detect the meeting end and was stopped by hand. Next step: log the full exception (`repr(e)` plus the cause chain), and reuse one long-lived `httpx.AsyncClient` instead of opening one per check.
- **Monitor's 30-minute cap** needed two restarts. Restarting with `tail -n 2 -F` replays the last lines, so expect duplicates.

## Runs on 2026-11-11 (two pharmacy SME meetings)

Source: each run's `status.json`, the `## ` control lines in `digest.log`, and `start_time` values in `events.jsonl`.

| Run | Joined | Events | Ended by | `last_error` |
|---|---|---|---|---|
| SME Session, Pharmacy (`01M6CVQB…`) | about 29 min in | 343 events, 69 chunks, 0 late revisions | `meeting no longer active; feed ended` | none |
| Follow-up meeting (`01M75KMY…`) | about 1 min in; stopped and restarted once | 804 broadcasts across both connections | `stopped by user`, twice | none |

**Confirmed by these runs:**
- **Joining mid-meeting works, with no backlog.** In the first run the first chunk had `start_time` 1758s and arrived within a second of connecting. Nothing said before the join is delivered; the post-meeting Fireflies transcript is the only record of it.
- **`start_time` counts seconds from the meeting start.** Subtracting `start_time` from wall-clock arrival gives the same meeting start for every chunk in a run, and on both days. This was checked against wall-clock time, not against the Fireflies transcript.
- **End detection worked** in the first run: the active-meeting check saw the meeting go and ended the feed by itself. The `ConnectError` from 11-10 did not recur, but nothing has been fixed, so that issue stays open.
- **Zero late revisions** in both runs, so the 4s settle still holds.

**Found in the second run:**
- **A restart in the same meeting resets `status.json`.** After the restart it showed 253 events, which is the second connection's count, while `events.jsonl` held both connections' 804 broadcasts. Speech between the stop (10:49:53) and the restart (10:52:29) was not received. Read counts across a restart from `events.jsonl`, not `status.json`.

## Fixes (2026-11-11)

| Issue | Fix | Verified |
|---|---|---|
| Monitor cut long digest lines | `--max-chars` defaults to 400 (`DEFAULT_MAX_CHARS`) | Replaying the 11-10 run gives 114 lines, at most 400 characters (412 bytes). Not yet verified in a live Monitor |
| End check failed with a bare `ConnectError:` | One `httpx.AsyncClient` for the whole run; failures are logged with `describe_exc`: type, message or repr, and the cause chain. The error also goes to `last_error` | Unit test with a chained exception; two calls through one shared client against the real endpoint. The cause of the 11-10 failures is still unknown, and the next failure will show it |
| Monitor's 30-minute cap; re-arming replayed lines | `ff-live follow` prints digest lines and saves its position in `follow.offset` after each one, so a re-armed follow neither repeats nor misses lines. It exits by itself once the feed has finished, or reports a feed process that died without a final state | Unit tests. The cap itself is Monitor's (1,800,000 ms) and can't be changed from here |
| `status.json` counts reset on a restart | A new run in the same meeting directory carries over events, chunks, settled, late revisions and digests, and adds `runs` and `first_started_at` | Unit test |

Still open: speech said during a restart or disconnect is never delivered. That is the server's behaviour.

## Still not verified

- Exact latency from speech to digest. It felt like the settle time plus up to one window; I haven't measured it.
- Whether `start_time` matches the timestamps in the Fireflies post-meeting transcript.

## Live run checklist

1. Pick a meeting with no PHI and confirm that with John. The Fireflies bot must be in it.
2. `ff-live meetings`, then `ff-live run -m <id> --window 30`, then a Monitor running `ff-live follow -m <id>` (timeout 1800000; re-arm the same command on expiry).
3. Afterwards, check `status.json`: `late_revisions`, `events`/`chunks`, `runs`, and `last_error`. Compare `transcript.txt` with the Fireflies transcript once it syncs.
4. Keep `events.jsonl` (no PHI) as a real fixture for `ff-live replay` and tests, then update this document's Not verified list.
