"""Fireflies transcript sync with local SQLite storage."""

import asyncio
import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from .auth import get_api_key
from .client import FirefliesClient, Transcript
from .srt import sentences_to_srt, sentences_to_txt

# Default storage location
DEFAULT_STORAGE_DIR = Path.home() / ".claude" / "personal" / "transcripts"
DEFAULT_DB_PATH = DEFAULT_STORAGE_DIR / "fireflies.db"


class TranscriptSync:
    """Manages local sync of Fireflies transcripts."""

    def __init__(
        self,
        storage_dir: Path | None = None,
        db_path: Path | None = None,
    ):
        """Initialize sync manager.

        Args:
            storage_dir: Directory for storing transcripts (default: ~/.claude/personal/transcripts)
            db_path: Path to SQLite database (default: storage_dir/fireflies.db)
        """
        self.storage_dir = storage_dir or DEFAULT_STORAGE_DIR
        self.db_path = db_path or (self.storage_dir / "fireflies.db")

        # Ensure directories exist
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        (self.storage_dir / "srt").mkdir(exist_ok=True)
        (self.storage_dir / "txt").mkdir(exist_ok=True)
        (self.storage_dir / "json").mkdir(exist_ok=True)

        self._init_db()

    def _init_db(self):
        """Initialize SQLite database schema."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS transcripts (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    date INTEGER,
                    duration REAL,
                    organizer_email TEXT,
                    participants TEXT,
                    summary TEXT,
                    action_items TEXT,
                    srt_path TEXT,
                    txt_path TEXT,
                    json_path TEXT,
                    synced_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_transcripts_date ON transcripts(date DESC)
            """)
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5(
                    id,
                    title,
                    summary,
                    action_items,
                    participants,
                    content='transcripts',
                    content_rowid='rowid'
                )
            """)
            conn.commit()

    def _get_last_sync(self) -> datetime | None:
        """Get last sync timestamp."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT value FROM sync_state WHERE key = 'last_sync'"
            ).fetchone()
            if row:
                return datetime.fromisoformat(row[0])
            return None

    def _set_last_sync(self, dt: datetime):
        """Update last sync timestamp."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sync_state (key, value) VALUES ('last_sync', ?)",
                (dt.isoformat(),),
            )
            conn.commit()

    def _save_transcript(self, transcript: Transcript) -> dict[str, str]:
        """Save transcript files locally.

        Args:
            transcript: Transcript with sentences

        Returns:
            Dict with paths to saved files
        """
        safe_title = "".join(
            c if c.isalnum() or c in " -_" else "_" for c in transcript.title
        )[:50]
        date_prefix = transcript.date.strftime("%Y%m%d") if transcript.date else "unknown"
        base_name = f"{date_prefix}_{transcript.id}_{safe_title}"

        paths = {}

        # Save SRT
        if transcript.sentences:
            srt_content = sentences_to_srt(transcript.sentences)
            srt_path = self.storage_dir / "srt" / f"{base_name}.srt"
            srt_path.write_text(srt_content)
            paths["srt"] = str(srt_path)

            # Save plain text
            txt_content = sentences_to_txt(transcript.sentences)
            txt_path = self.storage_dir / "txt" / f"{base_name}.txt"
            txt_path.write_text(txt_content)
            paths["txt"] = str(txt_path)

        # Save JSON metadata
        json_data = {
            "id": transcript.id,
            "title": transcript.title,
            "date": transcript.date.isoformat() if transcript.date else None,
            "duration": transcript.duration,
            "organizer_email": transcript.organizer_email,
            "participants": transcript.participants,
            "summary": transcript.summary,
            "action_items": transcript.action_items,
            "transcript_url": transcript.transcript_url,
        }
        json_path = self.storage_dir / "json" / f"{base_name}.json"
        json_path.write_text(json.dumps(json_data, indent=2))
        paths["json"] = str(json_path)

        return paths

    def _index_transcript(self, transcript: Transcript, paths: dict[str, str]):
        """Index transcript in SQLite database.

        Args:
            transcript: Transcript data
            paths: Paths to saved files
        """
        now = int(datetime.now().timestamp() * 1000)
        date_ms = int(transcript.date.timestamp() * 1000) if transcript.date else None

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO transcripts (
                    id, title, date, duration, organizer_email, participants,
                    summary, action_items, srt_path, txt_path, json_path,
                    synced_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transcript.id,
                    transcript.title,
                    date_ms,
                    transcript.duration,
                    transcript.organizer_email,
                    ",".join(transcript.participants),
                    transcript.summary,
                    "\n".join(transcript.action_items) if transcript.action_items else None,
                    paths.get("srt"),
                    paths.get("txt"),
                    paths.get("json"),
                    now,
                    now,
                ),
            )
            # Update FTS index
            conn.execute(
                """
                INSERT OR REPLACE INTO transcript_fts (
                    rowid, id, title, summary, action_items, participants
                ) SELECT rowid, id, title, summary, action_items, participants
                FROM transcripts WHERE id = ?
                """,
                (transcript.id,),
            )
            conn.commit()

    async def sync(
        self,
        days: int = 5,
        force: bool = False,
        progress_callback=None,
    ) -> dict:
        """Sync transcripts from Fireflies.

        Args:
            days: Number of days to sync (default: 5)
            force: Force re-sync even if already synced
            progress_callback: Optional callback(current, total, transcript)

        Returns:
            Sync statistics
        """
        api_key = get_api_key()
        if not api_key:
            raise ValueError("Fireflies API key not configured")

        last_sync = self._get_last_sync()
        sync_from = datetime.now() - timedelta(days=days)

        # Use last sync time if more recent and not forcing
        if last_sync and last_sync > sync_from and not force:
            sync_from = last_sync

        stats = {
            "total": 0,
            "synced": 0,
            "skipped": 0,
            "errors": 0,
            "error_details": [],
        }

        async with FirefliesClient(api_key) as client:
            # Get list of transcripts with pagination (API max is 50 per request)
            transcripts = []
            skip = 0
            batch_size = 50

            while True:
                batch = await client.list_transcripts(
                    limit=batch_size,
                    skip=skip,
                    from_date=sync_from,
                )
                if not batch:
                    break
                transcripts.extend(batch)
                if len(batch) < batch_size:
                    break  # No more results
                skip += batch_size
                await asyncio.sleep(0.5)  # Rate limit: avoid API throttling

            stats["total"] = len(transcripts)

            for i, t in enumerate(transcripts):
                if progress_callback:
                    progress_callback(i + 1, stats["total"], t)

                try:
                    # Check if already synced
                    with sqlite3.connect(self.db_path) as conn:
                        existing = conn.execute(
                            "SELECT id FROM transcripts WHERE id = ?", (t.id,)
                        ).fetchone()
                        if existing and not force:
                            stats["skipped"] += 1
                            continue

                    # Fetch full transcript with sentences
                    full_transcript = await client.get_transcript(
                        t.id, include_sentences=True
                    )

                    # Save files and index
                    paths = self._save_transcript(full_transcript)
                    self._index_transcript(full_transcript, paths)
                    stats["synced"] += 1

                    # Rate limit: small delay between transcript fetches
                    await asyncio.sleep(0.3)

                except Exception as e:
                    stats["errors"] += 1
                    stats["error_details"].append(f"{t.id}: {e}")

        self._set_last_sync(datetime.now())
        return stats

    def search(
        self,
        query: str,
        limit: int = 20,
    ) -> list[dict]:
        """Search local transcripts using FTS.

        Args:
            query: Search query
            limit: Maximum results

        Returns:
            List of matching transcript metadata
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            # FTS5 search
            rows = conn.execute(
                """
                SELECT t.* FROM transcripts t
                JOIN transcript_fts fts ON t.id = fts.id
                WHERE transcript_fts MATCH ?
                ORDER BY t.date DESC
                LIMIT ?
                """,
                (query, limit),
            ).fetchall()

            return [dict(row) for row in rows]

    def list_transcripts(
        self,
        days: int | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """List synced transcripts.

        Args:
            days: Filter to last N days
            limit: Maximum results

        Returns:
            List of transcript metadata
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            if days:
                cutoff = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)
                rows = conn.execute(
                    """
                    SELECT * FROM transcripts
                    WHERE date >= ?
                    ORDER BY date DESC
                    LIMIT ?
                    """,
                    (cutoff, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM transcripts
                    ORDER BY date DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()

            return [dict(row) for row in rows]

    def get_transcript_content(
        self,
        transcript_id: str,
        format: Literal["srt", "txt", "json"] = "txt",
    ) -> str | None:
        """Get transcript content from local storage.

        Args:
            transcript_id: Transcript ID
            format: File format to retrieve

        Returns:
            File content or None if not found
        """
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                f"SELECT {format}_path FROM transcripts WHERE id = ?",
                (transcript_id,),
            ).fetchone()

            if row and row[0]:
                path = Path(row[0])
                if path.exists():
                    return path.read_text()

        return None

    def stats(self) -> dict:
        """Get sync statistics.

        Returns:
            Dict with storage statistics
        """
        with sqlite3.connect(self.db_path) as conn:
            total = conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
            last_sync = self._get_last_sync()

            # Calculate storage size
            storage_size = sum(
                f.stat().st_size
                for f in self.storage_dir.rglob("*")
                if f.is_file()
            )

        return {
            "total_transcripts": total,
            "last_sync": last_sync.isoformat() if last_sync else None,
            "storage_dir": str(self.storage_dir),
            "storage_size_mb": round(storage_size / (1024 * 1024), 2),
        }


def run_sync(days: int = 5, force: bool = False, verbose: bool = True) -> dict:
    """Run sync operation (convenience function).

    Args:
        days: Days to sync
        force: Force re-sync
        verbose: Print progress

    Returns:
        Sync stats
    """
    sync = TranscriptSync()

    def progress(current, total, transcript):
        if verbose:
            print(f"[{current}/{total}] {transcript.title[:50]}...")

    return asyncio.run(sync.sync(days=days, force=force, progress_callback=progress))
