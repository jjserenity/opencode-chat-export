"""
opencode-chat-export — MCP server to export OpenCode chat history to Markdown.

Supports OpenCode 1.x and 2.x (including 2.4+ where message parts live in a
separate ``session_part`` table).

Install:
    pip install opencode-chat-export

Add to ~/.config/opencode/opencode.jsonc:
    "chat-export": {
      "type": "local",
      "command": ["opencode-chat-export"],
      "enabled": false
    }

CLI usage:
    opencode-chat-export list
    opencode-chat-export export <session_id>
    opencode-chat-export export-current
    opencode-chat-export export-all
"""

from .server import main

__all__ = ["main"]
