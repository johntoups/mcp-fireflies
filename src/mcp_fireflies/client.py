"""Fireflies.ai GraphQL API client."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

GRAPHQL_ENDPOINT = "https://api.fireflies.ai/graphql"


@dataclass
class Transcript:
    """Meeting transcript data."""

    id: str
    title: str
    date: datetime | None
    duration: float | None  # minutes
    organizer_email: str | None
    participants: list[str]
    transcript_url: str | None
    summary: str | None
    action_items: list[str]
    sentences: list[dict] | None  # Full transcript sentences


def _normalize_action_items(raw: str | list[str] | None) -> list[str]:
    """Return action items as a list of non-empty lines.

    The Fireflies API returns summary.action_items as one Markdown string, not a
    list; treating that string as a list splits it into single characters.
    """
    if not raw:
        return []
    if isinstance(raw, str):
        return [line for line in raw.splitlines() if line.strip()]
    return [str(item) for item in raw if str(item).strip()]


@dataclass
class MeetingAnalytics:
    """Meeting analytics data."""

    id: str
    title: str
    duration: float | None
    speaker_talk_time: dict[str, float]  # speaker -> minutes
    word_count: int | None
    questions_count: int | None


class FirefliesClient:
    """Async client for Fireflies GraphQL API."""

    def __init__(self, api_key: str):
        """Initialize client with API key.

        Args:
            api_key: Fireflies API key
        """
        self.api_key = api_key
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client:
            await self._client.aclose()

    async def _query(self, query: str, variables: dict | None = None) -> dict[str, Any]:
        """Execute GraphQL query.

        Args:
            query: GraphQL query string
            variables: Query variables

        Returns:
            Response data

        Raises:
            httpx.HTTPError: On request failure
            ValueError: On GraphQL errors
        """
        if not self._client:
            raise RuntimeError("Client not initialized. Use 'async with' context manager.")

        payload = {"query": query}
        if variables:
            payload["variables"] = variables

        response = await self._client.post(GRAPHQL_ENDPOINT, json=payload)
        response.raise_for_status()

        data = response.json()
        if "errors" in data:
            raise ValueError(f"GraphQL errors: {data['errors']}")

        return data.get("data", {})

    async def list_transcripts(
        self,
        limit: int = 20,
        skip: int = 0,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
    ) -> list[Transcript]:
        """List recent meeting transcripts.

        Args:
            limit: Maximum number of transcripts to return
            skip: Number of transcripts to skip (pagination)
            from_date: Filter transcripts after this date
            to_date: Filter transcripts before this date

        Returns:
            List of transcript summaries
        """
        query = """
        query Transcripts($limit: Int, $skip: Int) {
            transcripts(limit: $limit, skip: $skip) {
                id
                title
                date
                duration
                organizer_email
                participants
                transcript_url
                summary {
                    overview
                    action_items
                }
            }
        }
        """

        variables = {"limit": limit, "skip": skip}
        data = await self._query(query, variables)

        transcripts = []
        for t in data.get("transcripts", []):
            # Parse date
            date = None
            if t.get("date"):
                try:
                    # Fireflies returns epoch milliseconds
                    date = datetime.fromtimestamp(int(t["date"]) / 1000)
                except (ValueError, TypeError):
                    pass

            # Filter by date if specified
            if date:
                if from_date and date < from_date:
                    continue
                if to_date and date > to_date:
                    continue

            summary_data = t.get("summary") or {}
            transcripts.append(
                Transcript(
                    id=t["id"],
                    title=t.get("title") or "Untitled",
                    date=date,
                    duration=t.get("duration"),
                    organizer_email=t.get("organizer_email"),
                    participants=t.get("participants") or [],
                    transcript_url=t.get("transcript_url"),
                    summary=summary_data.get("overview"),
                    action_items=_normalize_action_items(summary_data.get("action_items")),
                    sentences=None,
                )
            )

        return transcripts

    async def get_transcript(self, transcript_id: str, include_sentences: bool = True) -> Transcript:
        """Get full transcript by ID.

        Args:
            transcript_id: Transcript ID
            include_sentences: Whether to include full transcript sentences

        Returns:
            Full transcript data
        """
        sentences_field = "sentences { text speaker_name start_time end_time }" if include_sentences else ""

        query = f"""
        query Transcript($id: String!) {{
            transcript(id: $id) {{
                id
                title
                date
                duration
                organizer_email
                participants
                transcript_url
                summary {{
                    overview
                    action_items
                    shorthand_bullet
                }}
                {sentences_field}
            }}
        }}
        """

        data = await self._query(query, {"id": transcript_id})
        t = data.get("transcript")

        if not t:
            raise ValueError(f"Transcript not found: {transcript_id}")

        date = None
        if t.get("date"):
            try:
                date = datetime.fromtimestamp(int(t["date"]) / 1000)
            except (ValueError, TypeError):
                pass

        summary_data = t.get("summary") or {}
        return Transcript(
            id=t["id"],
            title=t.get("title") or "Untitled",
            date=date,
            duration=t.get("duration"),
            organizer_email=t.get("organizer_email"),
            participants=t.get("participants") or [],
            transcript_url=t.get("transcript_url"),
            summary=summary_data.get("overview"),
            action_items=_normalize_action_items(summary_data.get("action_items")),
            sentences=t.get("sentences"),
        )

    async def search_transcripts(self, keyword: str, limit: int = 20) -> list[Transcript]:
        """Search transcripts by keyword.

        Note: Fireflies API doesn't have native search, so this fetches recent
        transcripts and filters client-side. For production use, consider
        fetching more transcripts or implementing server-side search.

        Args:
            keyword: Search keyword
            limit: Maximum results to return

        Returns:
            Matching transcripts
        """
        # Fetch transcripts to search through (API max is 50)
        transcripts = await self.list_transcripts(limit=50)

        keyword_lower = keyword.lower()
        matches = []

        for t in transcripts:
            # Search in title, summary, and action items
            searchable = " ".join(
                filter(
                    None,
                    [
                        t.title,
                        t.summary,
                        " ".join(t.action_items),
                        " ".join(t.participants),
                    ],
                )
            ).lower()

            if keyword_lower in searchable:
                matches.append(t)
                if len(matches) >= limit:
                    break

        return matches

    async def get_meeting_analytics(self, transcript_id: str) -> MeetingAnalytics:
        """Get analytics for a meeting.

        Args:
            transcript_id: Transcript ID

        Returns:
            Meeting analytics data
        """
        query = """
        query Transcript($id: String!) {
            transcript(id: $id) {
                id
                title
                duration
                sentences {
                    speaker_name
                    start_time
                    end_time
                }
            }
        }
        """

        data = await self._query(query, {"id": transcript_id})
        t = data.get("transcript")

        if not t:
            raise ValueError(f"Transcript not found: {transcript_id}")

        # Calculate speaker talk time from sentences
        speaker_time: dict[str, float] = {}
        sentences = t.get("sentences") or []

        for s in sentences:
            speaker = s.get("speaker_name") or "Unknown"
            start = s.get("start_time") or 0
            end = s.get("end_time") or 0
            duration_sec = (end - start) / 1000  # Convert ms to seconds

            if speaker in speaker_time:
                speaker_time[speaker] += duration_sec
            else:
                speaker_time[speaker] = duration_sec

        # Convert to minutes
        speaker_time_minutes = {k: round(v / 60, 1) for k, v in speaker_time.items()}

        return MeetingAnalytics(
            id=t["id"],
            title=t.get("title") or "Untitled",
            duration=t.get("duration"),
            speaker_talk_time=speaker_time_minutes,
            word_count=sum(len(s.get("text", "").split()) for s in sentences),
            questions_count=sum(1 for s in sentences if "?" in s.get("text", "")),
        )
