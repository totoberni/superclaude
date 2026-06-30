#!/usr/bin/env python3
"""plan_index.py <abs-path-to-plan.md> --campaign <slug> [--status <s>]

Builds a compact searchable index card for a superclaude plan and upserts
it to the memory DB via memory_db.py.

Upserts to: --tier instance --type project --name plan-index-<slug>
(no --agent flag — omitting it avoids a duplicate entry at a different path).

Prints: upserted id + card body.
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


MEMORY_DB = Path("~/.claude/scripts/memory/memory_db.py").expanduser()
VENV_PYTHON = Path("~/.claude/.venv/bin/python").expanduser()


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def extract_goal_summary(lines: list[str], max_lines: int = 6) -> list[str]:
    """Return up to max_lines lines from the ## Goal section."""
    in_goal = False
    goal_lines = []
    for line in lines:
        if re.match(r"^##\s+Goal", line):
            in_goal = True
            continue
        if in_goal:
            if re.match(r"^##\s+", line):
                break
            stripped = line.strip()
            if stripped:
                goal_lines.append(stripped)
            if len(goal_lines) >= max_lines:
                break
    return goal_lines


def extract_phases(lines: list[str]) -> list[str]:
    """Return list of phase titles from ## Phase N or ### PN headings.

    Requires a phase IDENTIFIER — a number ('Phase 2', 'P0', 'P6') or a
    single uppercase letter ('Phase E', a pre-sequenced phase) — so that
    non-phase headings like '### Phase index' (lowercase 'index') are excluded.
    """
    phases = []
    for line in lines:
        if re.match(r"^#{2,3}\s+(Phase\s+(?:\d+|[A-Z])|P\d+)\b", line):
            title = re.sub(r"^#{2,3}\s+", "", line).strip()
            phases.append(title)
    return phases


def extract_human_gates(lines: list[str]) -> list[str]:
    """Return lines containing 🚪 (human gate markers)."""
    gates = []
    for line in lines:
        if "🚪" in line:
            stripped = line.strip()
            # Collapse markdown table pipes for readability
            if stripped.startswith("|"):
                # Extract non-empty cells
                cells = [c.strip() for c in stripped.strip("|").split("|") if c.strip()]
                gates.append("  ".join(cells))
            else:
                gates.append(stripped)
    return gates


def build_card(
    plan_path: Path,
    plan_html_path: Path,
    campaign: str,
    status: str,
    lines: list[str],
) -> str:
    phases = extract_phases(lines)
    gates = extract_human_gates(lines)
    goal_lines = extract_goal_summary(lines)

    card_lines = [
        f"campaign: {campaign}",
        f"status: {status}",
        f"phase_count: {len(phases)}",
    ]
    for p in phases:
        card_lines.append(f"  phase: {p}")
    if gates:
        card_lines.append("human_gates:")
        for g in gates[:6]:  # cap to keep card compact
            card_lines.append(f"  - {g}")
    card_lines.append(f"plan_md: {plan_path}")
    card_lines.append(f"plan_html: {plan_html_path}")
    if goal_lines:
        card_lines.append("goal_summary:")
        for gl in goal_lines:
            card_lines.append(f"  {gl}")

    return "\n".join(card_lines)


def upsert(slug: str, card_body: str, description: str) -> str:
    """Shell to memory_db.py upsert; return the upserted id."""
    name = f"plan-index-{slug}"
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
    cmd = [
        str(VENV_PYTHON),
        str(MEMORY_DB),
        "upsert",
        "--tier", "instance",
        "--type", "project",
        "--name", name,
        "--description", description,
        "--text-stdin",
    ]
    result = subprocess.run(
        cmd,
        input=card_body,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        die(f"memory_db.py upsert failed:\n{result.stderr.strip()}")

    output = result.stdout.strip()
    # Try to extract the id from output like "Upserted: 42" or "id=42"
    m = re.search(r"(?:Upserted|id)[=:\s]+(\d+)", output, re.IGNORECASE)
    upserted_id = m.group(1) if m else output
    return upserted_id


def main():
    parser = argparse.ArgumentParser(
        description="Build and upsert a compact plan index card to memory DB."
    )
    parser.add_argument("plan_md", help="Absolute path to plan.md")
    parser.add_argument("--campaign", required=True, help="Campaign slug")
    parser.add_argument("--status", default="DRAFT", help="Plan status (default: DRAFT)")
    args = parser.parse_args()

    plan_path = Path(args.plan_md).resolve()
    if not plan_path.exists():
        die(f"plan.md not found: {plan_path}")
    if not plan_path.is_file():
        die(f"not a file: {plan_path}")

    try:
        source = plan_path.read_text(encoding="utf-8")
    except OSError as exc:
        die(f"cannot read {plan_path}: {exc}")

    lines = source.splitlines()
    plan_html_path = plan_path.parent / "plan.html"

    if not MEMORY_DB.exists():
        die(f"memory_db.py not found at {MEMORY_DB}")
    if not VENV_PYTHON.exists():
        die(f"venv python not found at {VENV_PYTHON}")

    card_body = build_card(plan_path, plan_html_path, args.campaign, args.status, lines)
    description = f"Plan index card for campaign {args.campaign} — status {args.status}; phases + gates + paths"

    upserted_id = upsert(args.campaign, card_body, description)

    print(f"Upserted id: {upserted_id}")
    print()
    print("Card body:")
    print(card_body)


if __name__ == "__main__":
    main()
