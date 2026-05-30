# opencode-chat-export

MCP server to export [OpenCode](https://opencode.ai) chat history to Markdown files.

## Features

- **Export the current conversation** — no need to know the session ID
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

## MCP Tools

### `export_current_session`
Export the currently active conversation — the session that was most recently updated. This is the tool you want when you say "导出我们现在的对话". No need to know the session ID.

**Parameters:**
- `output_dir` (optional, default: `~/opencode-exports/`)

### `export_session`
Export a specific session by its ID (full or prefix) or slug.

**Parameters:**
- `session_id` (required) — e.g. `ses_189058eb` or just `ses_1890`
- `output_dir` (optional)

### `export_recent`
Export the N most recently updated sessions.

**Parameters:**
- `count` (optional, default: `5`)
- `output_dir` (optional)

### `list_sessions`
List available sessions sorted by last update time (newest first). Useful for finding the session ID to pass to `export_session`.

**Parameters:**
- `limit` (optional, default: `20`)

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

The current session is identified by ordering sessions by `time_updated` (last modified time) in descending order and picking the first one — this corresponds to the conversation you're currently working on.

## License

MIT
