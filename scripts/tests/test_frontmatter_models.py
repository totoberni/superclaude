"""Lint: il valore `model:` nel frontmatter di agenti e skill deve essere un alias."""
import os
import re
from pathlib import Path

import pytest

CLAUDE_HOME = Path(os.environ.get("CLAUDE_HOME_DIR", Path.home() / ".claude"))
# Override per puntare il test a una copia di staging della directory agents
AGENTS_DIR = Path(os.environ.get("AGENTS_DIR_OVERRIDE", CLAUDE_HOME / "agents"))
SKILLS_DIR = Path(os.environ.get("SKILLS_DIR_OVERRIDE", CLAUDE_HOME / "skills"))

ALLOWED_ALIASES = {"sonnet", "opus", "haiku", "inherit"}
MODEL_LINE = re.compile(r"^model:\s*(.*?)\s*$")


def frontmatter_model(path):
    """Restituisce il valore di `model:` nel frontmatter, oppure None se assente."""
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            return None
        match = MODEL_LINE.match(line)
        if match:
            return match.group(1).strip("'\"")
    return None


def collect_files():
    agent_files = sorted(AGENTS_DIR.rglob("*.md")) if AGENTS_DIR.is_dir() else []
    skill_files = sorted(SKILLS_DIR.glob("*/SKILL.md")) if SKILLS_DIR.is_dir() else []
    return agent_files + skill_files


FILES = collect_files()


def test_files_discovered():
    assert FILES, f"nessun file trovato in {AGENTS_DIR} / {SKILLS_DIR}"


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(p.anchor)))
def test_model_is_alias(path):
    value = frontmatter_model(path)
    if value is None:
        return
    assert value.lower() in ALLOWED_ALIASES, (
        f"{path}: model '{value}' non ammesso (consentiti: {sorted(ALLOWED_ALIASES)}; "
        "vietati suffisso [1m], fable, best, ID completi claude-*)"
    )
