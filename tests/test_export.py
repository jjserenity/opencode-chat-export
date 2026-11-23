"""Tests for the exporter's database adapters and Markdown rendering.

Run with:  python tests/test_export.py
"""

import base64
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from opencode_chat_export import server  # noqa: E402

T0 = 1_700_000_000_000


def _tool_part(status="completed", result="hi\n", error=None):
    state = {
        "status": status,
        "input": {"command": "echo hi"},
        "content": [{"type": "text", "text": result}],
        "metadata": {},
    }
    if error:
        state["error"] = {"type": "tool.execution", "message": error}
    return {
        "type": "tool",
        "id": "call_1",
        "name": "shell",
        "state": state,
        "time": {"created": T0 + 2},
    }


def _reasoning_part(text="Let me think"):
    return {"type": "reasoning", "text": text, "time": {"created": T0 + 1}}


def _text_part(text="Done."):
    return {"type": "text", "text": text}


def _make_v2_db(path, layout="parts", files=None, extra_sessions=()):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE project (id TEXT PRIMARY KEY, name TEXT, worktree TEXT);
        CREATE TABLE session_v2 (
            id TEXT PRIMARY KEY, project_id TEXT, workspace_id TEXT, parent_id TEXT,
            fork_session_id TEXT, slug TEXT, directory TEXT, path TEXT, title TEXT,
            version TEXT, cost REAL DEFAULT 0, tokens_input INTEGER DEFAULT 0,
            tokens_output INTEGER DEFAULT 0, tokens_reasoning INTEGER DEFAULT 0,
            tokens_cache_read INTEGER DEFAULT 0, tokens_cache_write INTEGER DEFAULT 0,
            agent TEXT, model TEXT, time_created INTEGER, time_updated INTEGER,
            time_archived INTEGER, time_busy INTEGER, time_idle INTEGER
        );
        CREATE TABLE session_message (
            id TEXT PRIMARY KEY, session_id TEXT, type TEXT, seq INTEGER,
            time_created INTEGER, time_updated INTEGER, data TEXT
        );
        CREATE TABLE session_part (
            message_id TEXT, session_id TEXT, ordinal INTEGER, type TEXT, call_id TEXT,
            time_created INTEGER, time_updated INTEGER, data TEXT,
            PRIMARY KEY (message_id, ordinal)
        );
        """
    )
    conn.execute("INSERT INTO project VALUES ('p1', 'Demo project', 'C:/demo')")
    conn.execute(
        """INSERT INTO session_v2 (id, project_id, slug, directory, title, version, cost,
               tokens_input, tokens_output, tokens_reasoning, agent, model,
               time_created, time_updated, time_busy)
           VALUES ('ses_test1', 'p1', 'demo-slug', 'C:/demo', 'Demo session', '2.4.0', 0.001,
               10, 5, 1, 'build', '{"providerID":"p","id":"m1"}', ?, ?, ?)""",
        (T0, T0 + 10, T0 + 9),
    )
    for session in extra_sessions:
        conn.execute(
            """INSERT INTO session_v2 (id, project_id, slug, directory, title, version,
                   time_created, time_updated, time_archived)
               VALUES (?, 'p1', ?, ?, ?, '2.4.0', ?, ?, ?)""",
            (
                session["id"],
                session["slug"],
                session.get("directory", "C:/demo"),
                session["title"],
                session["time_created"],
                session["time_updated"],
                session.get("time_archived"),
            ),
        )

    tool = _tool_part()
    content = [_reasoning_part(), _text_part(), tool]

    conn.execute(
        "INSERT INTO session_message VALUES ('msg_u1','ses_test1','user',1,?,?,?)",
        (T0, T0, json.dumps({"text": "Hello **world**", "files": files or []})),
    )
    conn.execute(
        "INSERT INTO session_message VALUES ('msg_a1','ses_test1','assistant',2,?,?,?)",
        (
            T0 + 1,
            T0 + 3,
            json.dumps(
                {
                    "agent": "build",
                    "model": {"id": "m1", "providerID": "p"},
                    "content": content if layout == "embedded" else [],
                    "cost": 0.001,
                    "tokens": {"input": 10, "output": 5, "reasoning": 1, "cache": {"read": 2, "write": 0}},
                    "finish": "stop",
                }
            ),
        ),
    )
    if layout == "parts":
        for ordinal, part in enumerate(content):
            conn.execute(
                "INSERT INTO session_part VALUES (?,?,?,?,?,?,?,?)",
                (
                    "msg_a1",
                    "ses_test1",
                    ordinal,
                    part["type"],
                    part.get("id"),
                    T0 + 1 + ordinal,
                    T0 + 1 + ordinal,
                    json.dumps(part),
                ),
            )
    conn.commit()
    conn.close()


def _make_v1_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE project (id TEXT PRIMARY KEY, name TEXT, worktree TEXT);
        CREATE TABLE session (
            id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT, slug TEXT,
            directory TEXT, path TEXT, title TEXT, version TEXT, cost REAL DEFAULT 0,
            tokens_input INTEGER DEFAULT 0, tokens_output INTEGER DEFAULT 0,
            tokens_reasoning INTEGER DEFAULT 0, tokens_cache_read INTEGER DEFAULT 0,
            tokens_cache_write INTEGER DEFAULT 0, agent TEXT, model TEXT,
            time_created INTEGER, time_updated INTEGER, time_archived INTEGER
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
            time_updated INTEGER, data TEXT
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER,
            time_updated INTEGER, data TEXT
        );
        """
    )
    conn.execute("INSERT INTO project VALUES ('p1', 'Old project', 'C:/old')")
    conn.execute(
        """INSERT INTO session (id, project_id, slug, directory, title, version, agent, model,
               time_created, time_updated)
           VALUES ('ses_old1','p1','old-slug','C:/old','Legacy session','1.19.5','build',
               '{"providerID":"p","id":"m0"}',?,?)""",
        (T0, T0 + 10),
    )
    conn.execute(
        "INSERT INTO message VALUES ('msg_u1','ses_old1',?,?,?)",
        (T0, T0, json.dumps({"role": "user"})),
    )
    conn.execute(
        "INSERT INTO message VALUES ('msg_a1','ses_old1',?,?,?)",
        (
            T0 + 1,
            T0 + 3,
            json.dumps(
                {
                    "role": "assistant",
                    "agent": "build",
                    "providerID": "p",
                    "modelID": "m0",
                    "cost": 0.002,
                    "tokens": {"input": 7, "output": 3},
                    "finish": "stop",
                }
            ),
        ),
    )
    parts = [
        ("part_1", "msg_u1", {"type": "text", "text": "Hello old"}),
        ("part_2", "msg_a1", {"type": "reasoning", "text": "Reasoning old"}),
        (
            "part_3",
            "msg_a1",
            {
                "type": "tool",
                "tool": "bash",
                "callID": "call_9",
                "state": {"status": "completed", "input": {"command": "ls"}, "output": "old output"},
            },
        ),
    ]
    for i, (pid, mid, data) in enumerate(parts):
        conn.execute(
            "INSERT INTO part VALUES (?,?,?,?,?,?)",
            (pid, mid, "ses_old1", T0 + i, T0 + i, json.dumps(data)),
        )
    conn.commit()
    conn.close()


class ExportTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.old_db = server.DB_PATH
        self.old_cwd = os.getcwd()

    def tearDown(self):
        server.DB_PATH = self.old_db
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _export(self, db_name="test.db", fmt=None, **kwargs):
        db_path = self.dir / db_name
        if fmt == "v1":
            _make_v1_db(db_path)
        else:
            _make_v2_db(db_path, **kwargs)
        server.DB_PATH = str(db_path)
        conn = server.get_db()
        try:
            session, error = server.resolve_session(conn, "ses_test1" if fmt != "v1" else "ses_old1")
            self.assertIsNone(error)
            out = self.dir / "out"
            result = server._export_one(conn, session, str(out))
            with open(result, encoding="utf-8") as fh:
                return result, fh.read()
        finally:
            conn.close()

    def test_v24_parts_table(self):
        _, md = self._export(layout="parts")
        self.assertIn("# Demo session", md)
        self.assertIn("Hello **world**", md)
        self.assertIn("> **Reasoning:**", md)
        self.assertIn("Let me think", md)
        self.assertIn("🔧 shell", md)
        self.assertIn('"command": "echo hi"', md)
        self.assertIn("hi", md)
        self.assertIn("Demo project", md)
        self.assertIn("p/m1", md)
        self.assertIn("10 in / 5 out", md)
        self.assertIn("- **Tool calls:** 1", md)

    def test_v23_embedded_content(self):
        _, md = self._export(db_name="embedded.db", layout="embedded")
        self.assertIn("Let me think", md)
        self.assertIn("🔧 shell", md)
        self.assertIn("hi", md)
        self.assertIn("Done.", md)

    def test_v1_legacy_tables(self):
        _, md = self._export(db_name="old.db", fmt="v1")
        self.assertIn("# Legacy session", md)
        self.assertIn("Hello old", md)
        self.assertIn("Reasoning old", md)
        self.assertIn("🔧 bash", md)
        self.assertIn("old output", md)

    def test_tool_error_is_rendered(self):
        db_path = self.dir / "err.db"
        _make_v2_db(db_path, layout="parts")
        conn = sqlite3.connect(db_path)
        error_part = _tool_part(status="error", result="", error="boom")
        conn.execute(
            "UPDATE session_part SET data = ? WHERE ordinal = 2",
            (json.dumps(error_part),),
        )
        conn.commit()
        conn.close()

        server.DB_PATH = str(db_path)
        conn = server.get_db()
        try:
            session, _ = server.resolve_session(conn, "ses_test1")
            md = server.render_session_md(session, server.get_session_tree(conn, session["id"]))
            self.assertIn("⚠️ ERROR", md)
            self.assertIn("boom", md)
        finally:
            conn.close()

    def test_attachments_are_saved(self):
        files = [
            {"name": "F:\\keys\\site.txt", "mime": "text/plain", "data": base64.b64encode(b"secret key").decode()},
            {"name": "shot.png", "mime": "image/png", "data": base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()},
        ]
        result, md = self._export(db_name="files.db", files=files)
        att_dir = Path(result).with_suffix(".attachments")
        self.assertTrue((att_dir / "site.txt").exists())
        self.assertTrue((att_dir / "shot.png").exists())
        self.assertIn("![shot.png](", md)
        self.assertIn("secret key", md)
        self.assertIn("_Attachment: shot.png (image/png", md)

    def test_list_filters_archived_and_children(self):
        db_path = self.dir / "list.db"
        _make_v2_db(
            db_path,
            extra_sessions=[
                {"id": "ses_arch", "slug": "arch", "title": "Archived", "time_created": T0, "time_updated": T0 + 20, "time_archived": T0 + 21},
            ],
        )
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO session_v2 (id, project_id, slug, title, parent_id, time_created, time_updated)
               VALUES ('ses_child','p1','child','Child','ses_test1',?,?)""",
            (T0, T0 + 30),
        )
        conn.commit()
        conn.close()

        server.DB_PATH = str(db_path)
        conn = server.get_db()
        try:
            ids = [s["id"] for s in server.list_sessions(conn)]
            self.assertEqual(ids, ["ses_test1"])
            all_ids = [s["id"] for s in server.list_sessions(conn, include_archived=True, include_children=True)]
            self.assertIn("ses_arch", all_ids)
            self.assertIn("ses_child", all_ids)
        finally:
            conn.close()

    def test_current_session_prefers_busy_in_cwd(self):
        db_path = self.dir / "current.db"
        _make_v2_db(db_path, extra_sessions=[])
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO session_v2 (id, project_id, slug, directory, title, time_created,
                   time_updated, time_busy)
               VALUES ('ses_other','p1','other','C:/elsewhere','Other',?,?,?)""",
            (T0, T0 + 100, T0 + 99),
        )
        conn.commit()
        conn.close()

        os.chdir(self.dir)
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE session_v2 SET directory = ? WHERE id = 'ses_test1'", (str(self.dir),))
        conn.commit()
        conn.close()

        server.DB_PATH = str(db_path)
        conn = server.get_db()
        try:
            session = server.find_current_session(conn)
            self.assertEqual(session["id"], "ses_test1")
        finally:
            conn.close()

    def test_resolve_prefix(self):
        db_path = self.dir / "resolve.db"
        _make_v2_db(db_path)
        server.DB_PATH = str(db_path)
        conn = server.get_db()
        try:
            session, error = server.resolve_session(conn, "ses_test")
            self.assertIsNone(error)
            self.assertEqual(session["id"], "ses_test1")
            session, error = server.resolve_session(conn, "demo-slug")
            self.assertIsNone(error)
            self.assertEqual(session["id"], "ses_test1")
            session, error = server.resolve_session(conn, "nope")
            self.assertIsNone(session)
            self.assertIn("not found", error)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
