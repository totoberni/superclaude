"""Controlla la policy degli strumenti dei worker w-*.

(a) Fallisce (exit 1) se un `w-*.md` ha ancora una riga `tools:` nel frontmatter
    o se il suo `disallowedTools` non contiene tutte le voci di deny universali.
(b) Scansiona i transcript recenti e segnala come WARNING (exit invariato) ogni
    prefisso di server MCP che non compare ne' nel deny universale ne' negli allowed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

CLAUDE_HOME = Path.home() / ".claude"
DEFAULT_AGENTS_DIR = CLAUDE_HOME / "agents"
DEFAULT_POLICY = CLAUDE_HOME / "config" / "worker-tool-policy.json"
PROJECTS_DIR = CLAUDE_HOME / "projects"
TRANSCRIPT_GLOBS = ("*/*.jsonl", "*/*/subagents/*.jsonl")

# Il nome del server e' tutto cio' che precede il secondo "__": segmenti separati
# da un solo underscore, poi "__" e il nome dello strumento.
MCP_TOOL_RE = re.compile(r"mcp__([A-Za-z0-9-]+(?:_[A-Za-z0-9-]+)*)__[A-Za-z0-9_-]+")
TOOLS_LINE_RE = re.compile(r"^tools\s*:")
DISALLOWED_RE = re.compile(r"^disallowedTools\s*:\s*(.*)$")
LIST_ITEM_RE = re.compile(r"^\s+-\s+(.+?)\s*$")


def load_policy(policy_path: Path) -> dict[str, list[str]]:
    with policy_path.open(encoding="utf-8") as fh:
        policy = json.load(fh)
    for key in ("universal_deny_builtins", "universal_deny_mcp", "allowed_mcp"):
        if not isinstance(policy.get(key), list):
            raise ValueError(f"{policy_path}: chiave '{key}' assente o non lista")
    return policy


def read_frontmatter(agent_path: Path) -> list[str]:
    lines = agent_path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return []
    for end, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return lines[1:end]
    return []


def _strip_scalar(value: str) -> str:
    return value.strip().strip("'\"")


def parse_disallowed(frontmatter: list[str]) -> set[str]:
    # Gestisce sia la forma inline (a, b o [a, b]) sia la lista YAML a blocchi.
    entries: set[str] = set()
    for idx, line in enumerate(frontmatter):
        match = DISALLOWED_RE.match(line)
        if not match:
            continue
        inline = match.group(1).strip()
        if inline:
            inline = inline.strip("[]")
            entries.update(_strip_scalar(v) for v in inline.split(",") if v.strip())
            continue
        for item_line in frontmatter[idx + 1:]:
            item = LIST_ITEM_RE.match(item_line)
            if item:
                entries.add(_strip_scalar(item.group(1)))
            elif item_line.strip() and not item_line.lstrip().startswith("#"):
                break
    return entries


def check_agents(agents_dir: Path, required: list[str]) -> list[str]:
    agent_files = sorted(agents_dir.glob("w-*.md"))
    if not agent_files:
        return [f"{agents_dir}: nessun file w-*.md trovato"]
    failures: list[str] = []
    for agent_path in agent_files:
        frontmatter = read_frontmatter(agent_path)
        if not frontmatter:
            failures.append(f"{agent_path}: frontmatter assente o non chiuso")
            continue
        if any(TOOLS_LINE_RE.match(line) for line in frontmatter):
            failures.append(f"{agent_path}: has a 'tools:' line")
        denied = parse_disallowed(frontmatter)
        missing = [entry for entry in required if entry not in denied]
        if missing:
            failures.append(f"{agent_path}: disallowedTools missing {len(missing)}: {', '.join(missing)}")
    return failures


def recent_transcripts(projects_dir: Path, days: int) -> list[Path]:
    cutoff = time.time() - days * 86400
    found: list[Path] = []
    for pattern in TRANSCRIPT_GLOBS:
        for path in projects_dir.glob(pattern):
            try:
                if path.stat().st_mtime >= cutoff:
                    found.append(path)
            except OSError:
                continue
    return sorted(found)


def scan_mcp_prefixes(transcripts: list[Path]) -> Counter[str]:
    # Lettura riga per riga: i transcript possono pesare centinaia di MB.
    prefixes: Counter[str] = Counter()
    for path in transcripts:
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "mcp__" not in line:
                        continue
                    for match in MCP_TOOL_RE.finditer(line):
                        prefixes[f"mcp__{match.group(1)}"] += 1
        except OSError as exc:
            print(f"WARNING: cannot read {path}: {exc}", file=sys.stderr)
    return prefixes


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate w-* tool policy and scan transcripts for unknown MCP servers.")
    parser.add_argument("--agents-dir", type=Path, default=DEFAULT_AGENTS_DIR)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--transcripts-days", type=int, default=7)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    policy = load_policy(args.policy.expanduser())
    required = policy["universal_deny_builtins"] + policy["universal_deny_mcp"]

    failures = check_agents(args.agents_dir.expanduser(), required)
    for failure in failures:
        print(f"FAIL {failure}")

    transcripts = recent_transcripts(PROJECTS_DIR, args.transcripts_days)
    prefixes = scan_mcp_prefixes(transcripts)
    known = set(policy["universal_deny_mcp"]) | set(policy["allowed_mcp"])
    unknown = sorted(p for p in prefixes if p not in known)
    for prefix in unknown:
        print(f"WARNING: MCP server {prefix} is in neither universal_deny_mcp nor allowed_mcp ({prefixes[prefix]} hits)")

    print(
        f"SUMMARY: agents_failures={len(failures)} transcripts_scanned={len(transcripts)} "
        f"mcp_prefixes_seen={len(prefixes)} unknown_mcp={len(unknown)}"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
