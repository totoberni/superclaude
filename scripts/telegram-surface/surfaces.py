"""Read-only data surfaces for the Telegram factory bot.

Every function here reads superclaude state and returns plain strings.
Hard guarantees relied on by the security review (docs/telegram-surface-security.md):
  - SQLite opened with ?mode=ro URIs only — writes are impossible at the driver level.
  - No subprocess / os.system / eval anywhere in this package.
  - The only caller-supplied input (agent name) is regex-validated, then bound
    as a SQL parameter — never interpolated.
"""

import os
import re
import sqlite3
import time
from pathlib import Path

HOME = Path.home()
BROKER_DB = HOME / ".claude" / "comms" / ".broker.db"
MEMORY_DB = HOME / ".claude" / "agent-memory" / ".memory.db"
TIMERS_DIR = HOME / ".claude" / "session-timers"

AGENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
TRUNC = 3500  # Telegram hard cap is 4096 chars/message; leave headroom


def _ro(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite DB strictly read-only (URI mode=ro)."""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _age(ts: int) -> str:
    delta = max(0, int(time.time()) - int(ts))
    if delta < 60:
        return "now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _truncate(text: str, limit: int = TRUNC) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated {len(text) - limit} chars]"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def active_sessions() -> list[str]:
    """One line per live session, from session-timer files + PID liveness."""
    lines = []
    if not TIMERS_DIR.is_dir():
        return lines
    for agent_file in sorted(TIMERS_DIR.glob("*.agent")):
        sid = agent_file.stem
        try:
            agent = agent_file.read_text().strip() or "?"
            pid_file = TIMERS_DIR / f"{sid}.pid"
            start_file = TIMERS_DIR / f"{sid}.start"
            if not pid_file.is_file():
                continue
            pid = int(pid_file.read_text().strip())
            if not _pid_alive(pid):
                continue
            elapsed = "?"
            if start_file.is_file():
                mins = (int(time.time()) - int(start_file.read_text().strip())) // 60
                elapsed = f"{mins}m"
            lines.append(f"  {agent} · {elapsed} · {sid[:8]}")
        except (ValueError, OSError):
            continue  # malformed timer files are skipped, never fatal
    return lines


def latest_reports(limit: int = 8) -> list[str]:
    """Latest RPT per from_agent from the broker, newest first."""
    lines = []
    with _ro(BROKER_DB) as db:
        rows = db.execute(
            """SELECT from_agent, ts, body FROM messages m
               WHERE kind='RPT'
                 AND ts = (SELECT MAX(ts) FROM messages
                           WHERE kind='RPT' AND from_agent = m.from_agent)
               GROUP BY from_agent ORDER BY ts DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    for agent, ts, body in rows:
        first = next((l.strip() for l in body.splitlines() if l.strip()), "")
        lines.append(f"  {agent} · {_age(ts)}\n    {first[:120]}")
    return lines


def status_text() -> str:
    sessions = active_sessions()
    reports = latest_reports()
    parts = [f"🟢 Active sessions ({len(sessions)})"]
    parts += sessions or ["  none"]
    parts.append("")
    parts.append("📨 Latest report per agent")
    parts += reports or ["  none"]
    return _truncate("\n".join(parts))


def plans_text() -> str:
    """Plan-card summaries from memory DB plan-index entries (non-archived)."""
    cards = []
    with _ro(MEMORY_DB) as db:
        rows = db.execute(
            "SELECT name, text FROM memories WHERE name LIKE 'plan-index-%' ORDER BY name"
        ).fetchall()
    for name, text in rows:
        fields = {}
        for line in text.splitlines():
            key, sep, val = line.partition(":")
            if sep and key.strip() in ("campaign", "status", "phase_count"):
                fields.setdefault(key.strip(), val.strip())
        status = fields.get("status", "?")
        if status.upper().startswith("ARCHIVED"):
            continue
        campaign = fields.get("campaign", name.removeprefix("plan-index-"))
        phases = fields.get("phase_count", "?")
        cards.append(f"▸ {campaign} ({phases} phases)\n    {status[:140]}")
    header = f"📋 Plans ({len(cards)} active)"
    return _truncate("\n".join([header] + (cards or ["  none"])))


def report_text(agent: str) -> str:
    """Full latest RPT body from one agent, truncated for Telegram."""
    agent = agent.strip()
    if not AGENT_RE.match(agent):
        return "Invalid agent name (allowed: letters, digits, - and _, max 32 chars)."
    with _ro(BROKER_DB) as db:
        row = db.execute(
            """SELECT ts, seq, body FROM messages
               WHERE kind='RPT' AND from_agent = ?
               ORDER BY ts DESC LIMIT 1""",
            (agent,),
        ).fetchone()
    if row is None:
        return f"No RPT found from '{agent}'."
    ts, seq, body = row
    header = f"📄 {agent} · RPT-{seq or '?'} · {_age(ts)}\n\n"
    return _truncate(header + body)
