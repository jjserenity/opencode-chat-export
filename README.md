# opencode-chat-export

**English** | [Русский](README.ru.md)

MCP server to export [OpenCode](https://opencode.ai) chat history to Markdown files.

Works with **OpenCode 1.x and 2.x**, including 2.4+ where messages are split into
`session_v2` / `session_message` / `session_part` tables.

## Features

- **Export the current conversation** — auto-detects the session you are in
  (busy session in the current project, falling back to the most recently updated one)
- **All storage layouts** — v1 (`session`/`message`/`part`), v2 with parts embedded in
  `session_message.data.content` (2.0–2.3), and v2.4+ with the separate `session_part` table
- Chronological ordering with interleaved user/assistant messages
- Tool calls rendered as collapsible `<details>` blocks with input, result and errors
- Model reasoning displayed as blockquotes
- File attachments (images are saved next to the Markdown and embedded, text files are
  inlined in a collapsible block)
- Code patches with syntax highlighting
- Per-session and per-message cost/token usage
- Child (subagent) and archived sessions are hidden by default, but can be exported

## Install

```
pip install opencode-chat-export
```

Or with uv:

```
uv tool install opencode-chat-export
```

## Setup

Add to your OpenCode config (`~/.config/opencode/opencode.jsonc`):

```
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

Export the currently active conversation — the session that is running right now.
No need to know the session ID. The session is detected in this order:

1. `OPENCODE_SESSION_ID` environment variable (if the MCP server inherits it)
2. a busy session whose directory matches the working directory
3. any busy session (`time_busy` set and not finished afterwards)
4. the most recently updated session in the working directory
5. the most recently updated session overall

**Parameters:**

- `output_dir` (optional, default: `~/opencode-exports/`)
- `session_id` (optional) — pass an ID, ID prefix or slug if the auto-detection
  picked the wrong session (for example when several windows are busy)

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

List available sessions sorted by last update time (newest first). Marks `busy`,
`child` (subagent) and `archived` sessions. Useful for finding the session ID to pass
to `export_session`.

**Parameters:**

- `limit` (optional, default: `20`)
- `include_archived` (optional, default: `false`)
- `include_children` (optional, default: `false`)

## CLI Usage

```
# List recent sessions
opencode-chat-export list

# List archived and subagent sessions too
opencode-chat-export list --include-archived --include-children

# Export the session you are working in
opencode-chat-export export-current

# Export a session by ID prefix or slug
opencode-chat-export export ses_189058eb

# Export all sessions
opencode-chat-export export-all -o ~/my-exports

# Skip attachment files
opencode-chat-export export-current --no-attachments
```

## How it works

The tool reads directly from OpenCode's SQLite database, auto-detecting the database
location on Windows (`%APPDATA%/ai.opencode.desktop/opencode.db`,
`~/.local/share/opencode/opencode.db`) and Linux/macOS
(`~/.local/share/opencode/opencode.db`). When several databases exist, the most
recently written one wins.

The storage layout is detected automatically:

| OpenCode version | Sessions | Messages | Parts |
|---|---|---|---|
| 1.x | `session` | `message` | `part` |
| 2.0 – 2.3 | `session_v2` | `session_message` | embedded in `session_message.data.content` |
| 2.4+ | `session_v2` | `session_message` | `session_part` |

The v2.4 layout is controlled by the `session_part_write` setting; when it is disabled
OpenCode falls back to whole-message writes, so both layouts can appear in one database.
The exporter checks `session_part` per message and falls back to the embedded content
when no part rows exist.

### Environment variables

- `OPENCODE_DB` — explicit path to `opencode.db` (checked first)
- `OPENCODE_EXPORT_DIR` — default output directory (default `~/opencode-exports`)
- `OPENCODE_SESSION_ID` — used by `export_current_session` when available
- `OPENCODE_EXPORT_MAX_TOOL_OUTPUT` — max characters of a tool result (default `5000`)

## Development

```
python tests/test_export.py
```

The tests build temporary databases for all three layouts (v1, v2 embedded, v2.4 parts)
and verify rendering, attachments, filters and session resolution.

## License

MIT
