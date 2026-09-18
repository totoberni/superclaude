"""Test "a morso" per ssot-lint.py (port pytest di test-ssot-lint.sh).

Autocontenuti: ogni caso costruisce un corpus finto sotto tmp_path e non tocca
mai ~/.claude. Il linter viene eseguito come processo, come fa chi lo usa davvero.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

LINT = Path(__file__).resolve().parents[1] / "ssot-lint.py"


def _load_lint():
    # il nome del file contiene un trattino: non si puo' importare con `import`
    spec = importlib.util.spec_from_file_location("ssot_lint", LINT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HAS_SH = _load_lint().find_sh() is not None
needs_sh = pytest.mark.skipif(not HAS_SH, reason="no POSIX sh available for ground_truth checks")


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def registry(ground_truth_dir: str) -> str:
    return (
        "- id: test-thinking-doctrine\n"
        "  canonical_home: rules/13-worker-first-mandate.md:Critical Note\n"
        "  defining_marker: '^##\\s+Critical Note$'\n"
        "  forbidden_pattern: 'thinking is NOT inherited \\(retired\\)'\n"
        "\n"
        "- id: test-cwd-fact\n"
        "  canonical_home: CLAUDE.md\n"
        f"  ground_truth: 'test -d \"{ground_truth_dir}\"'\n"
    )


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """Corpus minimo e pulito, con il suo registro. Ogni test parte da una copia nuova."""
    root = tmp_path / "root"
    write(root / "CLAUDE.md", "# Fixture root\n\nSee `rules/13-worker-first-mandate.md` for the thinking doctrine.\n")
    write(
        root / "rules" / "13-worker-first-mandate.md",
        "# Worker First Mandate (fixture)\n\n## Critical Note\n\nThinking is inherited from the session in this fixture doctrine.\n",
    )
    write(root / "skills" / "_shared" / "verdict-schema.md", "# verdict schema (fixture)\n\nNothing interesting here.\n")
    write(
        root / "agents" / "meta.md",
        "# meta (fixture)\n\nPointer only: see `rules/13-worker-first-mandate.md` § Critical Note.\n"
        "SOT: `skills/_shared/verdict-schema.md`\n",
    )
    write(root / "docs" / "notes.md", "# docs notes (fixture)\n\nNothing load-bearing.\n")
    # barre in avanti: il comando ground_truth gira sotto sh (Git Bash su Windows)
    write(root / "meta" / "concept-registry.yaml", registry(root.as_posix()))
    return root


def run_lint(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(LINT), "--root", str(root)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(text)


def test_clean_fixture_exits_zero(corpus):
    res = run_lint(corpus)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "SSOT-LINT: clean" in res.stdout


def test_redefined_marker_names_the_file(corpus):
    append(corpus / "agents" / "meta.md", "\n## Critical Note\n\nA second, competing definition landed here by mistake.\n")
    res = run_lint(corpus)
    assert res.returncode != 0
    assert "re-defined outside canonical_home: agents/meta.md" in res.stdout


def test_forbidden_pattern_hit_outside_home(corpus):
    append(corpus / "docs" / "notes.md", "\nSomeone wrote: thinking is NOT inherited (retired) in the wrong place.\n")
    res = run_lint(corpus)
    assert res.returncode != 0
    assert "forbidden_pattern hit outside canonical_home: docs/notes.md" in res.stdout


def test_broken_sot_pointer(corpus):
    append(corpus / "docs" / "notes.md", "\nSOT: `rules/does-not-exist.md`\n")
    res = run_lint(corpus)
    assert res.returncode != 0
    assert "docs/notes.md" in res.stdout
    assert "unresolved target 'rules/does-not-exist.md'" in res.stdout


@needs_sh
def test_ground_truth_mismatch(corpus):
    write(corpus / "meta" / "concept-registry.yaml", registry(corpus.as_posix() + "/this-directory-does-not-exist"))
    res = run_lint(corpus)
    assert res.returncode != 0
    assert "[test-cwd-fact] ground_truth failed" in res.stdout


def test_pointer_ignore_matches_with_native_separators(corpus):
    # regressione Windows: la chiave di POINTER_IGNORES usa "/", os.path.relpath restituiva "\"
    write(
        corpus / "skills" / "research" / "references" / "report.md",
        "# report (fixture)\n\nFor the numbers see `docs/reprod-notes.md §M3` in your own project.\n",
    )
    res = run_lint(corpus)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "1 ignored" in res.stdout
