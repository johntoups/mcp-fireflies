"""MCP server for Fireflies.ai meeting transcripts."""

import asyncio
import json
import sys
from datetime import datetime

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    TextContent,
    Tool,
)

from .auth import get_api_key
from .client import FirefliesClient

# Initialize MCP server
server = Server("mcp-fireflies")


def format_transcript_summary(t) -> str:
    """Format transcript for display."""
    date_str = t.date.strftime("%Y-%m-%d %H:%M") if t.date else "Unknown date"
    duration_str = f"{t.duration:.0f} min" if t.duration else "Unknown duration"
    participants_str = ", ".join(t.participants[:5])
    if len(t.participants) > 5:
        participants_str += f" (+{len(t.participants) - 5} more)"

    lines = [
        f"**{t.title}**",
        f"- ID: `{t.id}`",
        f"- Date: {date_str}",
        f"- Duration: {duration_str}",
        f"- Participants: {participants_str}",
    ]

    if t.summary:
        lines.append(f"- Summary: {t.summary[:200]}...")

    if t.action_items:
        lines.append(f"- Action Items: {len(t.action_items)}")

    return "\n".join(lines)


def format_full_transcript(t) -> str:
    """Format full transcript with sentences."""
    lines = [
        f"# {t.title}",
        "",
        f"**Date:** {t.date.strftime('%Y-%m-%d %H:%M') if t.date else 'Unknown'}",
        f"**Duration:** {t.duration:.0f} minutes" if t.duration else "",
        f"**Participants:** {', '.join(t.participants)}",
        "",
    ]

    if t.summary:
        lines.extend(["## Summary", "", t.summary, ""])

    if t.action_items:
        lines.extend(["## Action Items", ""])
        for item in t.action_items:
            lines.append(f"- {item}")
        lines.append("")

    if t.sentences:
        lines.extend(["## Transcript", ""])
        current_speaker = None
        for s in t.sentences:
            speaker = s.get("speaker_name") or "Unknown"
            text = s.get("text", "")

            if speaker != current_speaker:
                lines.append(f"\n**{speaker}:**")
                current_speaker = speaker

            lines.append(text)

    return "\n".join(lines)


def format_analytics(a) -> str:
    """Format meeting analytics."""
    lines = [
        f"# Analytics: {a.title}",
        "",
        f"**Duration:** {a.duration:.0f} minutes" if a.duration else "",
        f"**Total Words:** {a.word_count:,}" if a.word_count else "",
        f"**Questions Asked:** {a.questions_count}" if a.questions_count else "",
        "",
        "## Speaker Talk Time",
        "",
    ]

    # Sort speakers by talk time descending
    sorted_speakers = sorted(a.speaker_talk_time.items(), key=lambda x: x[1], reverse=True)
    for speaker, minutes in sorted_speakers:
        lines.append(f"- {speaker}: {minutes} min")

    return "\n".join(lines)


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    return [
        Tool(
            name="list_transcripts",
            description="List recent meeting transcripts from Fireflies.ai",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of transcripts to return (default: 20)",
                        "default": 20,
                    },
                    "days": {
                        "type": "integer",
                        "description": "Only show transcripts from the last N days",
                    },
                },
            },
        ),
        Tool(
            name="get_transcript",
            description="Get full transcript content for a specific meeting",
            inputSchema={
                "type": "object",
                "properties": {
                    "transcript_id": {
                        "type": "string",
                        "description": "The transcript ID (from list_transcripts)",
                    },
                    "include_full_transcript": {
                        "type": "boolean",
                        "description": "Include full word-by-word transcript (default: true)",
                        "default": True,
                    },
                },
                "required": ["transcript_id"],
            },
        ),
        Tool(
            name="get_summary",
            description="Get AI-generated summary and action items for a meeting",
            inputSchema={
                "type": "object",
                "properties": {
                    "transcript_id": {
                        "type": "string",
                        "description": "The transcript ID",
                    },
                },
                "required": ["transcript_id"],
            },
        ),
        Tool(
            name="search_meetings",
            description="Search meeting transcripts by keyword",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search keyword to find in meeting titles, summaries, and participants",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results to return (default: 10)",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_meeting_analytics",
            description="Get analytics for a meeting (speaker talk time, word count, etc.)",
            inputSchema={
                "type": "object",
                "properties": {
                    "transcript_id": {
                        "type": "string",
                        "description": "The transcript ID",
                    },
                },
                "required": ["transcript_id"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Handle tool calls."""
    api_key = get_api_key()
    if not api_key:
        return [
            TextContent(
                type="text",
                text="Error: Fireflies API key not configured.\n\n"
                "To configure, run: `fireflies-auth store`\n"
                "Or set environment variable: `export FIREFLIES_API_KEY=your_key`",
            )
        ]

    try:
        async with FirefliesClient(api_key) as client:
            if name == "list_transcripts":
                limit = arguments.get("limit", 20)
                days = arguments.get("days")

                from_date = None
                if days:
                    from_date = datetime.now().replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    from_date = from_date.replace(
                        day=from_date.day - days if from_date.day > days else 1
                    )

                transcripts = await client.list_transcripts(
                    limit=limit, from_date=from_date
                )

                if not transcripts:
                    return [TextContent(type="text", text="No transcripts found.")]

                output = [f"Found {len(transcripts)} transcripts:\n"]
                for t in transcripts:
                    output.append(format_transcript_summary(t))
                    output.append("")

                return [TextContent(type="text", text="\n".join(output))]

            elif name == "get_transcript":
                transcript_id = arguments["transcript_id"]
                include_sentences = arguments.get("include_full_transcript", True)

                transcript = await client.get_transcript(
                    transcript_id, include_sentences=include_sentences
                )
                return [TextContent(type="text", text=format_full_transcript(transcript))]

            elif name == "get_summary":
                transcript_id = arguments["transcript_id"]
                transcript = await client.get_transcript(
                    transcript_id, include_sentences=False
                )

                lines = [f"# Summary: {transcript.title}", ""]

                if transcript.summary:
                    lines.extend(["## Overview", "", transcript.summary, ""])

                if transcript.action_items:
                    lines.extend(["## Action Items", ""])
                    for item in transcript.action_items:
                        lines.append(f"- {item}")

                return [TextContent(type="text", text="\n".join(lines))]

            elif name == "search_meetings":
                query = arguments["query"]
                limit = arguments.get("limit", 10)

                transcripts = await client.search_transcripts(query, limit=limit)

                if not transcripts:
                    return [
                        TextContent(
                            type="text", text=f"No meetings found matching '{query}'."
                        )
                    ]

                output = [f"Found {len(transcripts)} meetings matching '{query}':\n"]
                for t in transcripts:
                    output.append(format_transcript_summary(t))
                    output.append("")

                return [TextContent(type="text", text="\n".join(output))]

            elif name == "get_meeting_analytics":
                transcript_id = arguments["transcript_id"]
                analytics = await client.get_meeting_analytics(transcript_id)
                return [TextContent(type="text", text=format_analytics(analytics))]

            else:
                return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


def main():
    """Run the MCP server."""
    async def run():
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(run())


if __name__ == "__main__":
    main()
