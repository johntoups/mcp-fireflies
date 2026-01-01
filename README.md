# mcp-fireflies

MCP server for [Fireflies.ai](https://fireflies.ai) meeting transcripts.

Access your meeting transcripts, summaries, and action items directly from Claude Code or any MCP-compatible client.

## Features

| Tool | Description |
|------|-------------|
| `list_transcripts` | List recent meeting transcripts with date filtering |
| `get_transcript` | Get full transcript content for a meeting |
| `get_summary` | Get AI-generated summary and action items |
| `search_meetings` | Search transcripts by keyword |
| `get_meeting_analytics` | Speaker talk time, word count, questions asked |

## Installation

```bash
# Install from source
git clone https://github.com/johntoups/mcp-fireflies.git
cd mcp-fireflies
pip install -e .
```

## Configuration

### Get your Fireflies API Key

1. Go to [Fireflies Integrations](https://app.fireflies.ai/integrations)
2. Find "Fireflies API" and click "Get API Key"
3. Copy your API key

### Store Credentials

**Option 1: System Keychain (Recommended)**
```bash
fireflies-auth store
# Prompts for API key and stores in macOS Keychain / Windows Credential Manager
```

**Option 2: Environment Variable**
```bash
export FIREFLIES_API_KEY=your_api_key_here
```

### Verify Configuration
```bash
fireflies-auth show
```

## Usage with Claude Code

Add to your Claude Code MCP settings (`~/.claude/settings.json` or project `.claude/settings.json`):

```json
{
  "mcpServers": {
    "fireflies": {
      "command": "mcp-fireflies"
    }
  }
}
```

Or with explicit Python path:

```json
{
  "mcpServers": {
    "fireflies": {
      "command": "python",
      "args": ["-m", "mcp_fireflies.server"]
    }
  }
}
```

### Passing API key via config (alternative to keychain/env)

```json
{
  "mcpServers": {
    "fireflies": {
      "command": "mcp-fireflies",
      "env": {
        "FIREFLIES_API_KEY": "your_key_here"
      }
    }
  }
}
```

## Example Usage

Once configured, you can ask Claude:

- "List my recent meetings"
- "Get the transcript from yesterday's standup"
- "Search for meetings about project planning"
- "What were the action items from the quarterly review?"
- "Show me speaker analytics for meeting abc123"

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Lint
ruff check src/
```

## API Reference

This server wraps the [Fireflies GraphQL API](https://docs.fireflies.ai/). The API provides:

- Transcript retrieval and search
- Meeting summaries and action items
- Speaker analytics and talk time
- Meeting metadata (participants, duration, etc.)

## License

MIT

## Contributing

Issues and PRs welcome at [github.com/johntoups/mcp-fireflies](https://github.com/johntoups/mcp-fireflies).
