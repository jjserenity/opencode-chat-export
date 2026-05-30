# opencode-chat-export

MCP server to export [OpenCode](https://opencode.ai) chat history to Markdown files.

## Features

- **`export_current_session`** — Export the currently active conversation (most recently updated)
- **`export_session`** — Export a specific session by ID or slug
- **`export_recent`** — Export the N most recent sessions
- **`list_sessions`** — Browse available sessions with timestamps
- Chronological ordering with interleaved user/assistant messages
- Tool calls rendered as collapsible `<details>` blocks with input/output
- Model reasoning displayed as blockquotes
- Code patches with syntax highlighting
- Cost and token usage summary

## Install

```bash
pip install opencode-chat-export
```

Or with uv:

```bash
uv tool install opencode-chat-export
```

## Setup

Add to your OpenCode config (`~/.config/opencode/opencode.jsonc`):

```jsonc
{
  "mcp": {
    "chat-export": {
      "type": "local",
      "command": ["opencode-chat-export"],
      "enabled": false
    }
  }
}
```

Set `"enabled": true` to activate, then ask your agent to export the current conversation.

## CLI Usage

```bash
# List recent sessions
opencode-chat-export list

# Export a session by ID prefix
opencode-chat-export export ses_189058eb

# Export all sessions
opencode-chat-export export-all -o ~/my-exports
```

## How it works

The tool reads directly from OpenCode's SQLite database, querying the `session`, `message`, and `part` tables to reconstruct conversations in chronological order. It auto-detects the database location on both Windows (`%APPDATA%/ai.opencode.desktop/opencode.db`) and Linux/macOS (`~/.local/share/opencode/opencode.db`).

## License

MIT
