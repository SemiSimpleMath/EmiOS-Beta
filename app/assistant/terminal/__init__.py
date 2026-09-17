"""Embedded terminal — runs the interactive Claude Code CLI in a PTY.

The browser gets a real terminal, not a chat transcript: `claude` starts with
no flags, so it picks up the machine's own defaults (auto mode on a Max plan),
`CLAUDE.md`, `.claude/hooks/`, and `.claude/skills/` exactly as it would in a
local shell.
"""
