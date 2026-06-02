"""MCP server for exporting OpenCode chat history to Markdown."""

import sqlite3
import json
import os
import re
import sys
import argparse
from datetime import datetime, timezone
from pathlib import Path

# ---- config ----

def _find_db() -> str:
    """Locate the opencode.db file across known install locations."""
    candidates = [
        os.path.expanduser(r"~\AppData\Roaming\ai.opencode.desktop\opencode.db"),
        os.path.expanduser(r"~\.local\share\opencode\opencode.db"),
    ]
    for p in candidates:
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            return p
    # fallback to first candidate that exists (even if empty)
    for p in candidates:
        if os.path.exists(p):
            return p
    return candidates[0]

DB_PATH = _find_db()
OUTPUT_DIR = os.path.expanduser("~/opencode-exports")


# ---- data access ----

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_session_tree(session_id: str) -> list[dict]:
    """Build a chronologically ordered conversation with all parts."""
    conn = get_db()
    c = conn.cursor()

    c.execute(
        "SELECT id, data, time_created FROM message WHERE session_id = ? ORDER BY time_created",
        (session_id,),
    )
    rows = c.fetchall()

    msg_map: dict[str, dict] = {}
    for r in rows:
        data = json.loads(r["data"])
        msg_map[r["id"]] = {
            "id": r["id"],
            "role": data.get("role", "unknown"),
            "parent_id": data.get("parentID"),
            "time_created": r["time_created"],
            "agent": data.get("agent", ""),
            "model_id": data.get("modelID", ""),
            "finish": data.get("finish", ""),
            "cost": data.get("cost", 0),
            "tokens": data.get("tokens", {}),
            "mode": data.get("mode", ""),
            "parts": [],
        }

    msg_ids = list(msg_map.keys())
    if msg_ids:
        placeholders = ",".join("?" for _ in msg_ids)
        c.execute(
            f"SELECT message_id, data, time_created FROM part WHERE message_id IN ({placeholders}) ORDER BY time_created",
            msg_ids,
        )
        for r in c.fetchall():
            pdata = json.loads(r["data"])
            ptype = pdata.get("type", "unknown")
            entry = {"type": ptype, "data": pdata, "time": r["time_created"]}
            if r["message_id"] in msg_map:
                msg_map[r["message_id"]]["parts"].append(entry)

    conn.close()

    return sorted(msg_map.values(), key=lambda m: m["time_created"] or 0)


# ---- markdown rendering ----

def fmt_ts(ms: int | None) -> str:
    if ms:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%H:%M:%S")
    return ""


def render_session_md(session: dict, messages: list[dict]) -> str:
    """Render a full session to Markdown."""
    title = session.get("title", "Untitled")
    slug = session.get("slug", "")
    model_raw = session.get("model", "{}")
    if isinstance(model_raw, str):
        try:
            model_name = json.loads(model_raw).get("id", model_raw)
        except json.JSONDecodeError:
            model_name = model_raw
    else:
        model_name = str(model_raw)

    agent = session.get("agent", "")
    cost = session.get("cost", 0)
    tokens_in = session.get("tokens_input", 0)
    tokens_out = session.get("tokens_output", 0)
    ts = session.get("time_created", 0)
    date_str = (
        datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        if ts else "unknown"
    )

    lines = [
        f"# {title}",
        "",
        f"- **Date:** {date_str}",
        f"- **Agent:** {agent}",
        f"- **Model:** {model_name}",
        f"- **Cost:** ${cost:.6f}" if cost else "- **Cost:** N/A",
        f"- **Tokens:** {tokens_in:,} in / {tokens_out:,} out",
    ]
    if slug:
        lines.append(f"- **Slug:** {slug}")
    lines.extend(["", "---", ""])

    for msg in messages:
        role = msg["role"]
        ts_str = fmt_ts(msg["time_created"])
        agent_label = msg.get("agent", "")
        label = f"### {role.title()}"
        if agent_label and role == "assistant":
            label += f" ({agent_label})"
        if ts_str:
            label += f" @ {ts_str}"
        lines.append(label)
        lines.append("")

        for part in msg["parts"]:
            ptype = part["type"]
            pdata = part["data"]

            if ptype == "text":
                lines.append(pdata.get("text", ""))
                lines.append("")

            elif ptype == "reasoning":
                text = pdata.get("text", "") or pdata.get("reasoning", "")
                if text:
                    lines.append("> **Reasoning:**")
                    lines.append(f"> {text}")
                    lines.append("")

            elif ptype == "tool":
                tool_name = pdata.get("tool", "unknown")
                state = pdata.get("state", {})
                tool_input = state.get("input", {})
                tool_result = state.get("output", "")
                tool_status = state.get("status", "")
                is_error = "error" in tool_status.lower() if tool_status else False

                if isinstance(tool_input, str):
                    try:
                        tool_input = json.loads(tool_input)
                    except json.JSONDecodeError:
                        pass

                lines.append("<details>")
                lines.append(
                    f"<summary>🔧 {tool_name}{' ⚠️ ERROR' if is_error else ''}</summary>"
                )
                lines.append("")
                if tool_input and tool_input != {}:
                    lines.append("**Input:**")
                    lines.append("```json")
                    lines.append(json.dumps(tool_input, indent=2, ensure_ascii=False))
                    lines.append("```")
                    lines.append("")
                if tool_result:
                    result_str = (
                        str(tool_result)
                        if isinstance(tool_result, str)
                        else json.dumps(tool_result, indent=2, ensure_ascii=False)
                    )
                    if len(result_str) > 5000:
                        result_str = result_str[:5000] + "\n\n... (truncated)"
                    lines.append("**Result:**")
                    lines.append("```")
                    lines.append(result_str)
                    lines.append("```")
                    lines.append("")
                lines.append("</details>")
                lines.append("")

            elif ptype == "file":
                path = pdata.get("path", "")
                content = pdata.get("content", "")
                lines.append(f"**File:** `{path}`")
                lines.append("")
                if content:
                    ext = os.path.splitext(path)[1] if path else ""
                    lang = ext.lstrip(".") if ext else ""
                    lines.append(f"```{lang}")
                    lines.append(content)
                    lines.append("```")
                    lines.append("")

            elif ptype == "patch":
                patch_text = pdata.get("text", "")
                path = pdata.get("path", "")
                if path:
                    lines.append(f"**Patch:** `{path}`")
                if patch_text:
                    lines.append("```diff")
                    lines.append(patch_text)
                    lines.append("```")
                    lines.append("")

        lines.append("")

    return "\n".join(lines)


# ---- helpers ----

def _resolve_session(session_id: str):
    """Look up a session by full ID, prefix, or slug."""
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM session WHERE id = ? OR id LIKE ?",
        (session_id, session_id + "%"),
    )
    session = c.fetchone()
    if not session:
        c.execute("SELECT * FROM session WHERE slug LIKE ?", (f"%{session_id}%",))
        session = c.fetchone()
    conn.close()
    return dict(session) if session else None


def _safe_filename(text: str, max_len: int = 60) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", text)[:max_len]


def _get_project_name(row: sqlite3.Row) -> str:
    """Resolve project name from a session row (with LEFT JOINed project fields)."""
    if row.get("project_name"):
        return row["project_name"]
    worktree = row.get("worktree") or ""
    directory = row.get("directory") or ""
    path = worktree or directory
    if path:
        return os.path.basename(os.path.normpath(path))
    return "?"


def _export_one(session: dict, out_dir: str) -> str:
    """Export one session to .md, return file path."""
    title = session.get("title", "Untitled")
    slug = session.get("slug", "untitled")
    ts = session.get("time_created", 0)

    messages = get_session_tree(session["id"])
    md = render_session_md(session, messages)

    prefix = ""
    if ts:
        prefix = datetime.fromtimestamp(ts / 1000).strftime("%Y%m%d_%H%M") + "_"
    fname = f"{prefix}{_safe_filename(slug, 30)}_{_safe_filename(title, 60)}.md"
    os.makedirs(out_dir, exist_ok=True)
    fpath = os.path.join(out_dir, fname)
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(md)
    return fpath


# ---- CLI ----

def cli_main():
    parser = argparse.ArgumentParser(
        description="Export OpenCode chat history to Markdown"
    )
    parser.add_argument(
        "action", nargs="?", choices=["list", "export", "export-all"],
        help="list: show sessions (with project name) | export: export one session | export-all: all sessions",
    )
    parser.add_argument("session_id", nargs="?", help="Session ID or slug prefix")
    parser.add_argument("--limit", type=int, default=20, help="Max entries to show")
    parser.add_argument("--output", "-o", default=OUTPUT_DIR, help="Output directory")
    args = parser.parse_args()

    conn = get_db()
    c = conn.cursor()

    if args.action == "list" or not args.action:
        c.execute(
            """SELECT s.id, s.title, s.slug, s.agent, s.time_updated,
                      s.project_id, s.directory,
                      p.name AS project_name, p.worktree
               FROM session s
               LEFT JOIN project p ON s.project_id = p.id
               ORDER BY s.time_updated DESC LIMIT ?""",
            (args.limit,),
        )
        rows = c.fetchall()
        print(f"{'#':>3}  {'ID':<14} {'Project':<18} {'Title':<36} {'Agent':<12} {'Updated'}")
        print("-" * 95)
        for i, r in enumerate(rows, 1):
            ts = r["time_updated"]
            ts_str = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M") if ts else "?"
            proj = _get_project_name(r)
            print(
                f"{i:>3}  {r['id'][:12]:<14} "
                f"{proj[:18]:<18} "
                f"{(r['title'] or 'Untitled')[:36]:<36} "
                f"{(r['agent'] or '?')[:12]:<12} {ts_str}"
            )

    elif args.action == "export":
        if not args.session_id:
            print("Error: need session_id")
            return
        session = _resolve_session(args.session_id)
        if not session:
            print(f"Session not found: {args.session_id}")
            return
        fpath = _export_one(session, args.output)
        print(f"Exported → {fpath}")

    elif args.action == "export-all":
        c.execute("SELECT id FROM session ORDER BY time_updated DESC")
        sids = [r[0] for r in c.fetchall()]
        for i, sid in enumerate(sids):
            c2 = conn.cursor()
            c2.execute("SELECT * FROM session WHERE id = ?", (sid,))
            session = c2.fetchone()
            if not session:
                continue
            fpath = _export_one(dict(session), args.output)
            print(f"[{i+1}/{len(sids)}] → {fpath}")

    conn.close()


# ---- MCP server ----

def main():
    """Entry point: runs as MCP server by default, CLI if args present."""
    # CLI mode when args are present
    if len(sys.argv) > 1 and sys.argv[1] in ("list", "export", "export-all"):
        cli_main()
        return

    # MCP server mode
    try:
        from mcp.server import Server, NotificationOptions
        from mcp.server.models import InitializationOptions
        import mcp.server.stdio
        import mcp.types as types
    except ImportError:
        print("MCP SDK not found. Install with: pip install mcp", file=sys.stderr)
        sys.exit(1)

    server = Server("opencode-chat-export")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="list_sessions",
                description="List recent OpenCode chat sessions (with project name)",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "Max sessions to show (default 20)",
                            "default": 20,
                        }
                    },
                },
            ),
            types.Tool(
                name="export_session",
                description="Export a specific session to Markdown",
                inputSchema={
                    "type": "object",
                    "required": ["session_id"],
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Session ID (full or prefix) or slug",
                        },
                        "output_dir": {
                            "type": "string",
                            "description": "Output directory (default: ~/opencode-exports/)",
                        },
                    },
                },
            ),
            types.Tool(
                name="export_recent",
                description="Export the most recent N sessions to Markdown",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "count": {
                            "type": "integer",
                            "description": "Number of recent sessions (default 5)",
                            "default": 5,
                        },
                        "output_dir": {
                            "type": "string",
                            "description": "Output directory",
                        },
                    },
                },
            ),
            types.Tool(
                name="export_current_session",
                description="Export the currently active session (most recently updated)",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "output_dir": {
                            "type": "string",
                            "description": "Output directory (default: ~/opencode-exports/)",
                        },
                    },
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        if name == "list_sessions":
            conn = get_db()
            c = conn.cursor()
            c.execute(
                """SELECT s.id, s.title, s.slug, s.model, s.agent, s.cost,
                          s.time_updated, s.project_id, s.directory,
                          p.name AS project_name, p.worktree
                   FROM session s
                   LEFT JOIN project p ON s.project_id = p.id
                   ORDER BY s.time_updated DESC LIMIT ?""",
                (arguments.get("limit", 20),),
            )
            rows = c.fetchall()
            conn.close()
            lines = [
                "| # | ID (prefix) | Project | Title | Agent | Updated |",
                "|---|------------|---------|-------|-------|---------|",
            ]
            for i, r in enumerate(rows, 1):
                ts = r["time_updated"]
                time_str = datetime.fromtimestamp(ts / 1000).strftime("%m-%d %H:%M") if ts else "?"
                proj = _get_project_name(r)
                lines.append(
                    f"| {i} | `{r['id'][:12]}` | {proj[:16]:<16} | "
                    f"{(r['title'] or 'Untitled')[:36]:<36} | "
                    f"{(r['agent'] or '?')[:12]:<12} | {time_str} |"
                )
            return [types.TextContent(type="text", text="\n".join(lines))]

        elif name == "export_session":
            session = _resolve_session(arguments["session_id"])
            if not session:
                return [types.TextContent(type="text", text=f"Session not found: {arguments['session_id']}")]
            out_dir = arguments.get("output_dir", OUTPUT_DIR)
            fpath = _export_one(session, out_dir)
            msg_count = len(get_session_tree(session["id"]))
            return [
                types.TextContent(
                    type="text",
                    text=f"Exported `{session['title']}` → {fpath}  \n{msg_count} messages",
                )
            ]

        elif name == "export_current_session":
            out_dir = arguments.get("output_dir", OUTPUT_DIR)
            # fall through to export_recent with count=1
            arguments["count"] = 1

        if name in ("export_recent", "export_current_session"):
            count = arguments.get("count", 5)
            out_dir = arguments.get("output_dir", OUTPUT_DIR)
            conn = get_db()
            c = conn.cursor()
            c.execute("SELECT id FROM session ORDER BY time_updated DESC LIMIT ?", (count,))
            rows = c.fetchall()
            conn.close()
            results = []
            for (sid,) in rows:
                conn2 = get_db()
                c2 = conn2.cursor()
                c2.execute("SELECT * FROM session WHERE id = ?", (sid,))
                session = c2.fetchone()
                conn2.close()
                if not session:
                    continue
                fpath = _export_one(dict(session), out_dir)
                results.append(f"  `{session['title']}` → {fpath}")
            return [
                types.TextContent(
                    type="text",
                    text=f"Exported {len(results)} session(s) to {out_dir}:\n" + "\n".join(results),
                )
            ]

        raise ValueError(f"Unknown tool: {name}")

    async def run():
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="opencode-chat-export",
                    server_version="1.0.0",
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                ),
            )

    import asyncio
    asyncio.run(run())


if __name__ == "__main__":
    main()
