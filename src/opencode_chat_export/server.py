"""MCP server for exporting OpenCode chat history to Markdown.

Supports both storage layouts used by OpenCode:

* v1 (``session`` / ``message`` / ``part`` tables)
* v2 (``session_v2`` / ``session_message`` / ``session_part`` tables)

The v2 layout has two flavours as well: older 2.x builds keep message parts
inside ``session_message.data.content``, while 2.4+ writes every part as its
own row in ``session_part`` (config: ``session_part_write``).
"""

import argparse
import base64
import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

# ---- config ----

MAX_TOOL_OUTPUT = int(os.environ.get("OPENCODE_EXPORT_MAX_TOOL_OUTPUT", "5000"))
MAX_ATTACHMENT_TEXT = 4000
IGNORED_PART_TYPES = {"step-start", "step-finish", "snapshot", "compaction", "patch-meta"}

_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _db_candidates() -> list[str]:
    candidates: list[str] = []
    env = os.environ.get("OPENCODE_DB")
    if env:
        candidates.append(os.path.expanduser(env))
    candidates.extend(
        [
            os.path.expanduser(r"~\AppData\Roaming\ai.opencode.desktop\opencode.db"),
            os.path.expanduser(r"~\.local\share\opencode\opencode.db"),
            os.path.expanduser(r"~\AppData\Local\opencode\opencode.db"),
        ]
    )
    # keep order, drop duplicates
    return list(dict.fromkeys(candidates))


def _db_mtime(path: str) -> float:
    """Latest mtime of the database and its WAL file (the WAL is newer while open)."""
    mtimes = []
    for suffix in ("", "-wal"):
        try:
            mtimes.append(os.path.getmtime(path + suffix))
        except OSError:
            pass
    return max(mtimes, default=0)


def _find_db() -> str:
    """Locate the most recently used opencode.db across known install locations."""
    candidates = _db_candidates()
    existing = [p for p in candidates if os.path.isfile(p) and os.path.getsize(p) > 0]
    if existing:
        return max(existing, key=_db_mtime)
    for p in candidates:
        if os.path.exists(p):
            return p
    return candidates[0]


DB_PATH = _find_db()
OUTPUT_DIR = os.path.expanduser(os.environ.get("OPENCODE_EXPORT_DIR", "~/opencode-exports"))


# ---- data access ----

def get_db() -> sqlite3.Connection:
    """Open the database (read-only when possible, so a running OpenCode is not disturbed)."""
    uri = "file:" + quote(str(Path(DB_PATH).resolve()).replace("\\", "/"), safe="/:") + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=10)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    except sqlite3.Error:
        conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _session_tables(conn: sqlite3.Connection) -> list[str]:
    """Session tables to read, newest layout first. Empty v2 tables are skipped."""
    available = _tables(conn)
    tables = [name for name in ("session_v2", "session") if name in available]
    if "session_v2" in tables and "session" in tables:
        try:
            if not conn.execute("SELECT 1 FROM session_v2 LIMIT 1").fetchone():
                tables.remove("session_v2")
        except sqlite3.Error:
            pass
    return tables or ["session"]


def _session_table(conn: sqlite3.Connection) -> str:
    return _session_tables(conn)[0]


_SESSION_SELECT = """
    SELECT s.*, p.name AS project_name, p.worktree AS project_worktree
    FROM {table} s
    LEFT JOIN project p ON s.project_id = p.id
"""


def _loads(raw, default=None):
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _query_sessions(
    conn: sqlite3.Connection,
    where: str = "",
    params: tuple = (),
    limit: int | None = None,
) -> list[dict]:
    table = _session_table(conn)
    sql = _SESSION_SELECT.format(table=table) + (" WHERE " + where if where else "")
    sql += " ORDER BY s.time_updated DESC"
    if limit:
        sql += " LIMIT ?"
        params = (*params, limit)
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        # project table missing in very old databases
        sql = f"SELECT s.* FROM {table} s" + (" WHERE " + where if where else "")
        sql += " ORDER BY s.time_updated DESC"
        if limit:
            sql += " LIMIT ?"
            params = (*params, limit)
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def list_sessions(
    conn: sqlite3.Connection,
    limit: int = 20,
    include_archived: bool = False,
    include_children: bool = False,
) -> list[dict]:
    clauses = []
    if not include_archived:
        clauses.append("s.time_archived IS NULL")
    if not include_children:
        clauses.append("(s.parent_id IS NULL OR s.parent_id = '')")
    return _query_sessions(conn, " AND ".join(clauses), (), limit)


def _session_rows(
    conn: sqlite3.Connection, where: str, params: tuple, limit: int = 10
) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for table in _session_tables(conn):
        sql = (
            _SESSION_SELECT.format(table=table)
            + f" WHERE {where} ORDER BY s.time_updated DESC LIMIT ?"
        )
        try:
            fetched = conn.execute(sql, (*params, limit)).fetchall()
        except sqlite3.Error:
            continue
        for r in fetched:
            row = dict(r)
            if row.get("id") not in seen:
                seen.add(row.get("id"))
                rows.append(row)
    rows.sort(key=lambda r: r.get("time_updated") or 0, reverse=True)
    return rows[:limit]


def _session_by(conn: sqlite3.Connection, column: str, value: str, limit: int = 10) -> list[dict]:
    return _session_rows(conn, f"s.{column} = ?", (value,), limit)


def _like_pattern(value: str, mode: str = "prefix") -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%" if mode == "prefix" else f"%{escaped}%"


def resolve_session(conn: sqlite3.Connection, session_id: str) -> tuple[dict | None, str | None]:
    """Find a session by full ID, ID prefix, or slug. Returns (session, error)."""
    needle = (session_id or "").strip()
    if not needle:
        return None, "Session ID is required."

    rows = _session_by(conn, "id", needle, limit=1)
    if rows:
        return rows[0], None

    prefix_rows = _session_rows(conn, "s.id LIKE ? ESCAPE '\\'", (_like_pattern(needle),), 10)
    if len(prefix_rows) == 1:
        return prefix_rows[0], None
    if len(prefix_rows) > 1:
        listing = "\n".join(f"- `{r['id']}` — {r.get('title') or 'Untitled'}" for r in prefix_rows)
        return None, f"Session ID prefix `{needle}` is ambiguous:\n{listing}"

    slug_rows = _session_by(conn, "slug", needle, limit=10)
    if not slug_rows:
        slug_rows = _session_rows(conn, "s.slug LIKE ? ESCAPE '\\'", (_like_pattern(needle, "contains"),), 10)
    if len(slug_rows) == 1:
        return slug_rows[0], None
    if len(slug_rows) > 1:
        listing = "\n".join(f"- `{r['id']}` — {r.get('title') or 'Untitled'}" for r in slug_rows)
        return None, f"Slug `{needle}` is ambiguous:\n{listing}"
    return None, f"Session not found: {needle}"


def _norm_path(path: str | None) -> str:
    if not path:
        return ""
    return os.path.normcase(os.path.normpath(path)).replace("\\", "/").rstrip("/")


def find_current_session(conn: sqlite3.Connection) -> dict | None:
    """Best guess at the conversation the caller is in right now.

    Priority:
    1. ``OPENCODE_SESSION_ID`` (set for shell tools, sometimes inherited by MCP)
    2. a busy session in the server's working directory
    3. any busy session (``time_busy`` set, not finished afterwards)
    4. the most recently updated session in the working directory
    5. the most recently updated session overall
    """
    env_id = os.environ.get("OPENCODE_SESSION_ID")
    if env_id:
        session, _ = resolve_session(conn, env_id)
        if session:
            return session

    table = _session_table(conn)
    columns = _columns(conn, table)
    cwd = _norm_path(os.getcwd())
    base_where = "s.time_archived IS NULL AND (s.parent_id IS NULL OR s.parent_id = '')"

    if "time_busy" in columns:
        busy = _query_sessions(
            conn,
            base_where + " AND s.time_busy IS NOT NULL AND (s.time_idle IS NULL OR s.time_idle < s.time_busy)",
            (),
            50,
        )
        for session in busy:
            if cwd and _norm_path(session.get("directory")) == cwd:
                return session
        if busy:
            return busy[0]

    recent = _query_sessions(conn, base_where, (), 50)
    for session in recent:
        if cwd and _norm_path(session.get("directory")) == cwd:
            return session
    return recent[0] if recent else None


def _get_parts(conn: sqlite3.Connection, session_id: str) -> dict[str, list[dict]]:
    """Load ``session_part`` rows (OpenCode 2.4+) grouped by message id."""
    if "session_part" not in _tables(conn):
        return {}
    parts: dict[str, list[dict]] = {}
    rows = conn.execute(
        """SELECT message_id, ordinal, type, time_created, data
           FROM session_part
           WHERE session_id = ?
           ORDER BY message_id, ordinal""",
        (session_id,),
    )
    for r in rows:
        data = _loads(r["data"], {})
        if not isinstance(data, dict):
            data = {"type": r["type"], "value": data}
        parts.setdefault(r["message_id"], []).append(
            {
                "type": data.get("type") or r["type"] or "unknown",
                "data": data,
                "time": data.get("time") or {"created": r["time_created"]},
            }
        )
    return parts


def _legacy_messages(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    """Read conversations stored in the v1 ``message`` / ``part`` tables."""
    if "message" not in _tables(conn):
        return []
    rows = conn.execute(
        "SELECT id, data, time_created FROM message WHERE session_id = ? ORDER BY time_created, id",
        (session_id,),
    ).fetchall()
    if not rows:
        return []

    messages: dict[str, dict] = {}
    for r in rows:
        data = _loads(r["data"], {})
        messages[r["id"]] = {
            "id": r["id"],
            "role": data.get("role", "unknown"),
            "type": data.get("role", "unknown"),
            "time_created": r["time_created"],
            "agent": data.get("agent", ""),
            "model": data.get("model") or {
                "providerID": data.get("providerID", ""),
                "id": data.get("modelID", ""),
            },
            "finish": data.get("finish", ""),
            "cost": data.get("cost", 0) or 0,
            "tokens": data.get("tokens") or {},
            "parts": [],
        }

    if "part" in _tables(conn) and messages:
        ids = list(messages)
        placeholders = ",".join("?" for _ in ids)
        for r in conn.execute(
            f"""SELECT message_id, data, time_created FROM part
                WHERE message_id IN ({placeholders})
                ORDER BY time_created, id""",
            ids,
        ):
            data = _loads(r["data"], {})
            if not isinstance(data, dict):
                continue
            entry = {
                "type": data.get("type", "unknown"),
                "data": data,
                "time": data.get("time") or {"created": r["time_created"]},
            }
            messages[r["message_id"]]["parts"].append(entry)

    return sorted(messages.values(), key=lambda m: m["time_created"] or 0)


def get_session_tree(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    """Build a chronologically ordered conversation with all parts."""
    messages: list[dict] = []
    if "session_message" in _tables(conn):
        rows = conn.execute(
            """SELECT id, type, seq, time_created, data
               FROM session_message
               WHERE session_id = ?
               ORDER BY seq, time_created, id""",
            (session_id,),
        ).fetchall()
        if rows:
            parts_by_message = _get_parts(conn, session_id)
            for r in rows:
                data = _loads(r["data"], {})
                embedded = data.get("content") if isinstance(data.get("content"), list) else []
                parts = parts_by_message.get(r["id"])
                if parts is None:
                    parts = [
                        {
                            "type": p.get("type", "unknown"),
                            "data": p,
                            "time": p.get("time") or {},
                        }
                        for p in embedded
                        if isinstance(p, dict)
                    ]
                messages.append(
                    {
                        "id": r["id"],
                        "role": "user" if r["type"] == "user" else "assistant",
                        "type": r["type"],
                        "time_created": r["time_created"],
                        "agent": data.get("agent", ""),
                        "model": data.get("model") or {},
                        "previous": data.get("previous") or {},
                        "finish": data.get("finish", ""),
                        "cost": data.get("cost", 0) or 0,
                        "tokens": data.get("tokens") or {},
                        "text": data.get("text", "") or "",
                        "description": data.get("description", "") or "",
                        "files": data.get("files") or [],
                        "metadata": data.get("metadata") or {},
                        "parts": parts,
                    }
                )
            return messages

    return _legacy_messages(conn, session_id)


# ---- markdown helpers ----

def fmt_ts(ms: int | None, fmt: str = "%H:%M:%S") -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000).strftime(fmt)


def _fmt_dt(ms: int | None) -> str:
    if not ms:
        return "unknown"
    dt = datetime.fromtimestamp(ms / 1000).astimezone()
    offset = dt.utcoffset() or timezone.utc.utcoffset(dt)
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return dt.strftime("%Y-%m-%d %H:%M:%S") + f" UTC{sign}{hours:02d}:{minutes:02d}"


def _fmt_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _quote(text: str) -> str:
    return "\n".join("> " + line if line.strip() else ">" for line in text.splitlines())


def _safe_filename(text: str, max_len: int = 60) -> str:
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(text or ""))
    text = re.sub(r"\s+", " ", text).strip(" .")
    if text.upper() in _WINDOWS_RESERVED:
        text = "_" + text
    return text[:max_len].rstrip(" .") or "untitled"


def _model_name(model) -> str:
    data = _loads(model, model)
    if not isinstance(data, dict):
        return str(model or "")
    model_id = data.get("id") or data.get("modelID") or ""
    provider = data.get("providerID") or ""
    variant = data.get("variant") or ""
    name = f"{provider}/{model_id}" if provider and model_id else (model_id or provider)
    return f"{name} ({variant})" if name and variant and variant != "default" else name


def _tokens_summary(tokens: dict) -> str:
    if not isinstance(tokens, dict) or not tokens:
        return ""
    cache = tokens.get("cache") or {}
    bits = []
    if tokens.get("input"):
        bits.append(f"{tokens['input']:,} in")
    if tokens.get("output"):
        bits.append(f"{tokens['output']:,} out")
    if tokens.get("reasoning"):
        bits.append(f"{tokens['reasoning']:,} reasoning")
    if cache.get("read"):
        bits.append(f"{cache['read']:,} cache read")
    if cache.get("write"):
        bits.append(f"{cache['write']:,} cache write")
    return " / ".join(bits)


def _truncate(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n... (truncated, {len(text):,} chars total)"


def _json_block(obj, lang: str = "json") -> list[str]:
    try:
        text = json.dumps(obj, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(obj)
    return [f"```{lang}", text, "```"]


def _tool_result_text(state: dict) -> str:
    chunks: list[str] = []
    for item in state.get("content") or []:
        if isinstance(item, str):
            chunks.append(item)
        elif isinstance(item, dict):
            itype = item.get("type")
            if itype == "text":
                chunks.append(item.get("text", ""))
            elif itype == "file":
                label = item.get("filename") or item.get("uri") or ""
                chunks.append(f"[file: {item.get('mime', 'unknown')}] {label}")
            else:
                chunks.append(json.dumps(item, ensure_ascii=False))
    text = "\n".join(c for c in chunks if c).strip()
    if not text:
        text = state.get("output") or (state.get("metadata") or {}).get("output") or ""
    if isinstance(text, (dict, list)):
        text = json.dumps(text, indent=2, ensure_ascii=False)
    return str(text)


def _unique_path(directory: str, name: str) -> str:
    stem, ext = os.path.splitext(name)
    candidate = os.path.join(directory, name)
    counter = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{stem}-{counter}{ext}")
        counter += 1
    return candidate


def _render_attachments(files: list, attachment_dir: str | None) -> list[str]:
    if not files:
        return []
    lines: list[str] = []
    for entry in files:
        if not isinstance(entry, dict):
            lines.append(f"- 📎 {entry}")
            continue
        name = entry.get("name") or entry.get("filename") or "attachment"
        mime = entry.get("mime") or "application/octet-stream"
        base_name = os.path.basename(str(name).replace("\\", "/"))
        raw = entry.get("data")
        size = 0
        blob: bytes | None = None
        if isinstance(raw, str) and raw:
            try:
                blob = base64.b64decode(raw, validate=False)
                size = len(blob)
            except (ValueError, TypeError):
                blob = None
        if not size and isinstance(raw, str):
            # some sources store plain text instead of base64
            blob = raw.encode("utf-8")
            size = len(blob)

        summary = f"{_safe_filename(base_name, 80)} ({mime}, {_fmt_bytes(size)})"
        if not blob or not attachment_dir:
            lines.append(f"- 📎 {summary}")
            continue

        os.makedirs(attachment_dir, exist_ok=True)
        target = _unique_path(attachment_dir, _safe_filename(base_name, 80))
        with open(target, "wb") as fh:
            fh.write(blob)
        rel = os.path.relpath(target, os.path.dirname(attachment_dir)).replace("\\", "/")
        if mime.startswith("image/"):
            lines.append(f"![{_safe_filename(base_name, 80)}]({rel})")
            lines.append("")
            lines.append(f"_Attachment: {summary}_")
        elif mime.startswith("text/") or mime in ("application/json", "application/xml"):
            text = blob.decode("utf-8", errors="replace")
            lines.append(f"**Attachment:** `{summary}`")
            lines.append("")
            lines.append("<details>")
            lines.append(f"<summary>Show content ({_fmt_bytes(size)})</summary>")
            lines.append("")
            lines.append("```")
            lines.append(_truncate(text, MAX_ATTACHMENT_TEXT))
            lines.append("```")
            lines.append("")
            lines.append("</details>")
        else:
            lines.append(f"- 📎 [{_safe_filename(base_name, 80)}]({rel}) ({mime}, {_fmt_bytes(size)})")
    return lines


def _render_part(part: dict, attachment_dir: str | None) -> list[str]:
    ptype = part.get("type", "unknown")
    data = part.get("data") or {}

    if ptype in IGNORED_PART_TYPES:
        return []

    if ptype == "text":
        return [(data.get("text") or "").rstrip(), ""]

    if ptype == "reasoning":
        text = data.get("text") or data.get("reasoning") or ""
        if not text:
            return []
        return ["> **Reasoning:**", _quote(text), ""]

    if ptype == "tool":
        tool_name = data.get("name") or data.get("tool") or "unknown"
        state = data.get("state") or {}
        status = state.get("status", "")
        tool_input = state.get("input") or {}
        result = _tool_result_text(state)
        error = state.get("error")
        title = state.get("title") or data.get("title") or ""

        badge = ""
        if status and status != "completed":
            badge = f" — {status}"
        if status == "error":
            badge = " — ⚠️ ERROR"
        summary = f"🔧 {tool_name}{f' — {title}' if title else ''}{badge}"

        lines = ["<details>", f"<summary>{summary}</summary>", ""]
        if tool_input:
            lines.append("**Input:**")
            lines.extend(_json_block(tool_input))
            lines.append("")
        if error:
            message = error.get("message") if isinstance(error, dict) else str(error)
            lines.append("**Error:**")
            lines.append("```")
            lines.append(_truncate(str(message or "")))
            lines.append("```")
            lines.append("")
        elif result:
            lines.append("**Result:**")
            lines.append("```")
            lines.append(_truncate(result))
            lines.append("```")
            lines.append("")
        lines.extend(["</details>", ""])
        return lines

    if ptype == "file":
        path = data.get("path") or data.get("filename") or ""
        content = data.get("content") or ""
        lines = [f"**File:** `{path}`", ""]
        if content:
            ext = os.path.splitext(path)[1].lstrip(".")
            lines.extend([f"```{ext}", content, "```", ""])
        return lines

    if ptype == "patch":
        patch_text = data.get("text") or ""
        path = data.get("path") or ""
        lines = []
        if path:
            lines.append(f"**Patch:** `{path}`")
        if patch_text:
            lines.extend(["```diff", patch_text, "```"])
        elif data.get("files"):
            lines.extend(_json_block(data["files"]))
        if lines:
            lines.append("")
        return lines

    if ptype == "attachment":
        return _render_attachments([data], attachment_dir)

    # Unknown part: keep it, collapsed, so nothing silently disappears.
    return [
        "<details>",
        f"<summary>📦 {ptype}</summary>",
        "",
        *_json_block(data),
        "",
        "</details>",
        "",
    ]


def render_session_md(session: dict, messages: list[dict], attachment_dir: str | None = None) -> str:
    """Render a full session to Markdown."""
    title = session.get("title") or "Untitled"
    slug = session.get("slug") or ""
    agent = session.get("agent") or ""
    model_name = _model_name(session.get("model"))
    cost = session.get("cost") or 0
    ts = session.get("time_created") or 0

    tool_calls = sum(
        1
        for msg in messages
        for part in msg.get("parts", [])
        if part.get("type") == "tool"
    )
    user_count = sum(1 for m in messages if m.get("type") == "user")
    assistant_count = sum(1 for m in messages if m.get("type") == "assistant")

    lines = [f"# {title}", ""]
    lines.append(f"- **Date:** {_fmt_dt(ts)}")
    lines.append(f"- **Session:** `{session.get('id', '')}`")
    if slug:
        lines.append(f"- **Slug:** {slug}")
    if agent:
        lines.append(f"- **Agent:** {agent}")
    if model_name:
        lines.append(f"- **Model:** {model_name}")
    if session.get("directory"):
        lines.append(f"- **Directory:** `{session['directory']}`")
    if session.get("project_name") or session.get("project_worktree"):
        lines.append(f"- **Project:** {session.get('project_name') or session.get('project_worktree')}")
    if session.get("parent_id"):
        lines.append(f"- **Parent session:** `{session['parent_id']}`")
    if session.get("fork_session_id"):
        lines.append(f"- **Forked from:** `{session['fork_session_id']}`")
    if session.get("time_archived"):
        lines.append("- **Archived:** yes")

    tokens_in = session.get("tokens_input") or 0
    tokens_out = session.get("tokens_output") or 0
    tokens_reasoning = session.get("tokens_reasoning") or 0
    cache_read = session.get("tokens_cache_read") or 0
    cache_write = session.get("tokens_cache_write") or 0
    if not any((tokens_in, tokens_out, tokens_reasoning, cache_read, cache_write, cost)):
        for msg in messages:
            tokens = msg.get("tokens") or {}
            cache = tokens.get("cache") or {}
            tokens_in += tokens.get("input", 0) or 0
            tokens_out += tokens.get("output", 0) or 0
            tokens_reasoning += tokens.get("reasoning", 0) or 0
            cache_read += cache.get("read", 0) or 0
            cache_write += cache.get("write", 0) or 0
            cost += msg.get("cost", 0) or 0

    if cost:
        lines.append(f"- **Cost:** ${cost:.6f}")
    token_bits = [f"{tokens_in:,} in", f"{tokens_out:,} out"]
    if tokens_reasoning:
        token_bits.append(f"{tokens_reasoning:,} reasoning")
    if cache_read:
        token_bits.append(f"{cache_read:,} cache read")
    if cache_write:
        token_bits.append(f"{cache_write:,} cache write")
    lines.append(f"- **Tokens:** {' / '.join(token_bits)}")
    lines.append(f"- **Messages:** {user_count + assistant_count} ({user_count} user, {assistant_count} assistant)")
    lines.append(f"- **Tool calls:** {tool_calls}")
    lines.extend(["", "---", ""])

    for msg in messages:
        mtype = msg.get("type") or msg.get("role") or "unknown"
        role = msg.get("role") or mtype
        ts_str = fmt_ts(msg.get("time_created"))

        if mtype == "idle":
            continue

        if mtype == "model-switched":
            model = _model_name(msg.get("model"))
            previous = _model_name(msg.get("previous"))
            if model:
                lines.append(f"> 🔄 Model switched{f' from {previous}' if previous else ''} to {model}")
                lines.append("")

        elif mtype == "system":
            data = msg.get("metadata") or {}
            description = msg.get("description") or data.get("notice") or "system"
            text = msg.get("text", "")
            lines.append("<details>")
            lines.append(f"<summary>⚙️ System — {description}</summary>")
            lines.append("")
            lines.append(_truncate(text))
            lines.append("")
            lines.append("</details>")
            lines.append("")

        elif mtype == "synthetic":
            data = msg.get("metadata") or {}
            description = msg.get("description") or data.get("source") or "synthetic"
            state = data.get("state") or ""
            lines.append("<details>")
            lines.append(f"<summary>🧪 Synthetic — {description}{f' ({state})' if state else ''}</summary>")
            lines.append("")
            lines.append(_truncate(msg.get("text", "")))
            lines.append("")
            lines.append("</details>")
            lines.append("")

        elif mtype not in ("user", "assistant"):
            # Unknown message type: keep it rather than dropping history.
            lines.append(f"### {mtype.title()}{f' @ {ts_str}' if ts_str else ''}")
            lines.append("")
            if msg.get("text"):
                lines.append(_truncate(msg["text"]))
                lines.append("")
            for part in msg.get("parts", []):
                lines.extend(_render_part(part, attachment_dir))
            lines.append("")

        else:
            label = f"### {role.title()}"
            if msg.get("agent") and role == "assistant":
                label += f" ({msg['agent']})"
            if ts_str:
                label += f" @ {ts_str}"
            lines.append(label)
            lines.append("")

            if role == "user":
                text = msg.get("text") or ""
                if text:
                    lines.append(text.rstrip())
                    lines.append("")
                files = msg.get("files") or (msg.get("metadata") or {}).get("attachments") or []
                attachment_lines = _render_attachments(files, attachment_dir)
                if attachment_lines:
                    lines.extend(attachment_lines)
                    lines.append("")

            for part in msg.get("parts", []):
                rendered = _render_part(part, attachment_dir)
                if rendered:
                    lines.extend(rendered)

            meta = []
            model_name = _model_name(msg.get("model"))
            if role == "assistant" and model_name:
                meta.append(model_name)
            token_text = _tokens_summary(msg.get("tokens") or {})
            if role == "assistant" and token_text:
                meta.append(token_text)
            if role == "assistant" and msg.get("cost"):
                meta.append(f"${msg['cost']:.6f}")
            if role == "assistant" and msg.get("finish") and msg["finish"] not in ("stop", "tool-calls"):
                meta.append(f"finish: {msg['finish']}")
            if meta:
                lines.append(f"<sub>{' · '.join(meta)}</sub>")
                lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---- export ----

def _export_one(conn: sqlite3.Connection, session: dict, out_dir: str, save_attachments: bool = True) -> str:
    """Export one session to .md, return file path."""
    ts = session.get("time_created") or 0
    prefix = datetime.fromtimestamp(ts / 1000).strftime("%Y%m%d_%H%M_") if ts else ""
    slug = session.get("slug") or "untitled"
    title = session.get("title") or "Untitled"
    fname = f"{prefix}{_safe_filename(slug, 30)}_{_safe_filename(title, 60)}.md"

    messages = get_session_tree(conn, session["id"])
    os.makedirs(out_dir, exist_ok=True)
    attachment_dir = None
    if save_attachments:
        attachment_dir = os.path.join(out_dir, os.path.splitext(fname)[0] + ".attachments")
        if os.path.isdir(attachment_dir):
            shutil.rmtree(attachment_dir, ignore_errors=True)

    markdown = render_session_md(session, messages, attachment_dir)
    fpath = os.path.join(out_dir, fname)
    with open(fpath, "w", encoding="utf-8", newline="\n") as f:
        f.write(markdown)
    return fpath


def _project_label(row: dict) -> str:
    name = row.get("project_name")
    if name:
        return name
    path = row.get("project_worktree") or row.get("worktree") or row.get("directory") or ""
    if path:
        return os.path.basename(os.path.normpath(path)) or path
    return "?"


def _is_busy(row: dict) -> bool:
    busy = row.get("time_busy")
    if not busy:
        return False
    idle = row.get("time_idle")
    return not idle or idle < busy


def _format_session_row(index: int, row: dict) -> str:
    updated = fmt_ts(row.get("time_updated"), "%Y-%m-%d %H:%M") or "?"
    flags = []
    if row.get("time_archived"):
        flags.append("archived")
    if row.get("parent_id"):
        flags.append("child")
    if _is_busy(row):
        flags.append("busy")
    flag_text = f" [{' '.join(flags)}]" if flags else ""
    return (
        f"{index:>3}  {str(row.get('id', ''))[:14]:<14} "
        f"{_project_label(row)[:18]:<18} "
        f"{(row.get('title') or 'Untitled')[:40]:<40} "
        f"{(row.get('agent') or '?')[:12]:<12} {updated}{flag_text}"
    )


# ---- CLI ----

def cli_main() -> None:
    parser = argparse.ArgumentParser(description="Export OpenCode chat history to Markdown")
    parser.add_argument(
        "action",
        nargs="?",
        choices=["list", "export", "export-current", "export-all"],
        help="list: show sessions | export: one session | export-current: active session | export-all: every session",
    )
    parser.add_argument("session_id", nargs="?", help="Session ID, ID prefix, or slug")
    parser.add_argument("--limit", type=int, default=20, help="Max entries to show")
    parser.add_argument("--output", "-o", default=OUTPUT_DIR, help="Output directory")
    parser.add_argument("--include-archived", action="store_true", help="Include archived sessions")
    parser.add_argument("--include-children", action="store_true", help="Include child (subagent) sessions")
    parser.add_argument("--no-attachments", action="store_true", help="Do not save attachments")
    args = parser.parse_args()

    if not os.path.isfile(DB_PATH) or os.path.getsize(DB_PATH) == 0:
        searched = "\n".join(f"  - {p}" for p in _db_candidates())
        print(f"OpenCode database not found. Searched:\n{searched}")
        return

    conn = get_db()
    try:
        if args.action in (None, "list"):
            rows = list_sessions(
                conn,
                args.limit,
                include_archived=args.include_archived,
                include_children=args.include_children,
            )
            print(f"Database: {DB_PATH}")
            print(f"{'#':>3}  {'ID':<14} {'Project':<18} {'Title':<40} {'Agent':<12} Updated")
            print("-" * 112)
            for i, row in enumerate(rows, 1):
                print(_format_session_row(i, row))

        elif args.action == "export":
            if not args.session_id:
                print("Error: need a session ID")
                return
            session, error = resolve_session(conn, args.session_id)
            if error:
                print(error)
                return
            fpath = _export_one(conn, session, args.output, not args.no_attachments)
            count = len(get_session_tree(conn, session["id"]))
            print(f"Exported → {fpath}  ({count} messages)")

        elif args.action == "export-current":
            session = find_current_session(conn)
            if not session:
                print("No sessions found")
                return
            fpath = _export_one(conn, session, args.output, not args.no_attachments)
            count = len(get_session_tree(conn, session["id"]))
            print(f"Exported → {fpath}  ({session.get('title') or 'Untitled'}, {count} messages)")

        elif args.action == "export-all":
            rows = list_sessions(
                conn,
                100000,
                include_archived=args.include_archived,
                include_children=args.include_children,
            )
            for i, row in enumerate(rows, 1):
                session, error = resolve_session(conn, row["id"])
                if error:
                    print(f"[{i}/{len(rows)}] {error}")
                    continue
                fpath = _export_one(conn, session, args.output, not args.no_attachments)
                print(f"[{i}/{len(rows)}] → {fpath}")
    finally:
        conn.close()


# ---- MCP server ----

def _mcp_tools(types):
    return [
        types.Tool(
            name="list_sessions",
            description="List recent OpenCode chat sessions (with project name and flags)",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max sessions to show (default 20)",
                        "default": 20,
                    },
                    "include_archived": {
                        "type": "boolean",
                        "description": "Include archived sessions (default false)",
                        "default": False,
                    },
                    "include_children": {
                        "type": "boolean",
                        "description": "Include child/subagent sessions (default false)",
                        "default": False,
                    },
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
            description=(
                "Export the active session (the conversation you are in). "
                "Auto-detects the busy session; pass session_id if the guess is wrong."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "output_dir": {
                        "type": "string",
                        "description": "Output directory (default: ~/opencode-exports/)",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Optional explicit session ID, prefix, or slug",
                    },
                },
            },
        ),
    ]


def _mcp_list_text(rows: list[dict]) -> str:
    lines = [
        "| # | ID (prefix) | Project | Title | Agent | Updated |",
        "|---|------------|---------|-------|-------|---------|",
    ]
    for i, row in enumerate(rows, 1):
        updated = fmt_ts(row.get("time_updated"), "%m-%d %H:%M") or "?"
        flags = []
        if row.get("time_archived"):
            flags.append("archived")
        if row.get("parent_id"):
            flags.append("child")
        if _is_busy(row):
            flags.append("busy")
        title = (row.get("title") or "Untitled")[:36]
        if flags:
            title += f" [{' '.join(flags)}]"
        lines.append(
            f"| {i} | `{str(row.get('id', ''))[:12]}` | {_project_label(row)[:16]:<16} | "
            f"{title:<36} | {(row.get('agent') or '?')[:12]:<12} | {updated} |"
        )
    return "\n".join(lines)


def main() -> None:
    """Entry point: runs as MCP server by default, CLI if args present."""
    if len(sys.argv) > 1 and sys.argv[1] in ("list", "export", "export-current", "export-all"):
        cli_main()
        return

    try:
        import mcp.server.stdio
        import mcp.types as types
        from mcp.server import Server
    except ImportError:
        print("MCP SDK not found. Install with: pip install mcp", file=sys.stderr)
        sys.exit(1)

    async def handle_call(name: str, arguments: dict | None) -> list:
        arguments = arguments or {}
        out_dir = os.path.expanduser(arguments.get("output_dir") or OUTPUT_DIR)

        if name == "list_sessions":
            conn = get_db()
            try:
                rows = list_sessions(
                    conn,
                    int(arguments.get("limit", 20)),
                    include_archived=bool(arguments.get("include_archived", False)),
                    include_children=bool(arguments.get("include_children", False)),
                )
                return [types.TextContent(type="text", text=_mcp_list_text(rows))]
            finally:
                conn.close()

        if name == "export_session":
            conn = get_db()
            try:
                session, error = resolve_session(conn, arguments.get("session_id", ""))
                if error:
                    return [types.TextContent(type="text", text=error)]
                fpath = _export_one(conn, session, out_dir)
                count = len(get_session_tree(conn, session["id"]))
                return [
                    types.TextContent(
                        type="text",
                        text=f"Exported `{session.get('title') or 'Untitled'}` → {fpath}  \n{count} messages",
                    )
                ]
            finally:
                conn.close()

        if name in ("export_recent", "export_current_session"):
            conn = get_db()
            try:
                if name == "export_current_session":
                    explicit = arguments.get("session_id")
                    if explicit:
                        session, error = resolve_session(conn, explicit)
                        if error:
                            return [types.TextContent(type="text", text=error)]
                        sessions = [session]
                    else:
                        session = find_current_session(conn)
                        if not session:
                            return [types.TextContent(type="text", text="No sessions found")]
                        sessions = [session]
                else:
                    sessions = list_sessions(conn, int(arguments.get("count", 5)))

                results = []
                for session in sessions:
                    fpath = _export_one(conn, session, out_dir)
                    results.append(f"  `{session.get('title') or 'Untitled'}` → {fpath}")
                return [
                    types.TextContent(
                        type="text",
                        text=f"Exported {len(results)} session(s) to {out_dir}:\n" + "\n".join(results),
                    )
                ]
            finally:
                conn.close()

        raise ValueError(f"Unknown tool: {name}")

    tools = _mcp_tools(types)
    import asyncio

    if hasattr(Server, "list_tools"):
        # MCP SDK 1.x: decorator-based low-level server.
        from mcp.server import NotificationOptions
        from mcp.server.models import InitializationOptions

        server = Server("opencode-chat-export")

        @server.list_tools()
        async def list_tools() -> list:
            return tools

        @server.call_tool()
        async def call_tool(name: str, arguments: dict) -> list:
            return await handle_call(name, arguments)

        async def run():
            async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
                await server.run(
                    read_stream,
                    write_stream,
                    InitializationOptions(
                        server_name="opencode-chat-export",
                        server_version="1.1.0",
                        capabilities=server.get_capabilities(
                            notification_options=NotificationOptions(),
                            experimental_capabilities={},
                        ),
                    ),
                )

    else:
        # MCP SDK 2.x: handlers are passed to the server constructor.
        async def on_list_tools(context, params):
            return types.ListToolsResult(tools=tools)

        async def on_call_tool(context, params):
            content = await handle_call(params.name, params.arguments or {})
            return types.CallToolResult(content=content)

        server = Server(
            "opencode-chat-export",
            version="1.1.0",
            on_list_tools=on_list_tools,
            on_call_tool=on_call_tool,
        )

        async def run():
            async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
                await server.run(
                    read_stream,
                    write_stream,
                    server.create_initialization_options(),
                )

    asyncio.run(run())


if __name__ == "__main__":
    main()
