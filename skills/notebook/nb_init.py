"""/notebook skill — bootstrap a notebook for skill use.

Creates .notebook/ next to the notebook with skill state; appends to
.gitattributes / .gitignore / .pre-commit-config.yaml; registers the
jupytext-regen merge driver in .git/config; installs the .git/hooks/pre-commit
rejection hook; injects the runtime probe cell as cell 0 if absent.

Refuses notebooks under /mnt/c/ (WSL hard rule) and bare repos.

V1.1 — addresses example-project-review-of-skill-v1.md issues:
  - V1-B3: detects project `.venv` and registers `<project>-venv` kernelspec via
           `python -m ipykernel install --user --name <project>-venv`. Writes
           the resolved kernel name to `<dir>/.notebook/kernel_name`.
  - V1-B4: writes a per-worktree marker `<dir>/.notebook/.merge-driver-installed`
           so `nb batch` can warn if the user runs from a worktree where init
           never ran. Plus prose update in SKILL.md.
  - V1-H3: WARNs loudly when the runtime-probe cell is injected at index 0
           (cell-shift effect on `at_position` references). `--no-probe-injection`
           opt-out flag.
  - V1-H4: detects installed heavy deps (qiskit, qiskit-aer, torch, scipy,
           sklearn) and writes a warm.py with the relevant import blocks
           uncommented, instead of the universal commented template.
  - V1-H7: appends `*.ipynb.lock` and `.notebook/snapshots/` to .gitignore.
  - V1-H16: `--migrate` flag implemented (re-runs all install steps idempotently).
  - V1-H17: explicit bare-repo refusal in nb_init (in addition to nb_io's check).
  - V1-H18: detects legacy scaffolders (scripts/*.py with raw json on .ipynb)
           and emits a deprecation hint pointing at `nb batch`.
  - V1-L2: notebook_lock acquired during init to prevent concurrent inits.
  - V1-L4: merge_regen.py chmod 0o755 on install so it remains executable.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import nbformat

from nb_io import atomic_write_ipynb, canonical_repo_root, notebook_lock

SKILL_DIR = Path(__file__).resolve().parent
TEMPLATES = SKILL_DIR / "templates"
SKILL_VERSION = (SKILL_DIR / "version").read_text().strip()


def _refuse_mnt_c(path: Path) -> None:
    p = str(path.resolve())
    if p.startswith("/mnt/c/") or p.startswith("/mnt/d/"):
        sys.exit(
            f"[notebook] REFUSE: {path} is under /mnt — 9P, no inotify, "
            "order-of-magnitude slower than ext4. Move project to /home."
        )


def _refuse_bare_repo(repo_root: Path) -> None:
    """V1-H17: explicit bare-repo refusal here (in addition to nb_io's check)."""
    try:
        bare = subprocess.check_output(
            ["git", "-C", str(repo_root), "rev-parse", "--is-bare-repository"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except subprocess.CalledProcessError:
        return  # not in a git repo at all; canonical_repo_root would have caught it
    if bare == "true":
        sys.exit(f"[notebook] REFUSE: {repo_root} is a bare repository; "
                 "notebook ops require a working tree.")


def _ensure_notebook_dir(nb_path: Path, force: bool, migrate: bool) -> Path:
    nbd = nb_path.parent / ".notebook"
    if nbd.exists() and not (force or migrate):
        existing = (nbd / "version").read_text().strip() if (nbd / "version").exists() else "?"
        if existing != SKILL_VERSION:
            sys.exit(
                f"[notebook] {nbd} already initialised at v{existing}; "
                f"current skill v{SKILL_VERSION}. Run `nb init --migrate` to upgrade "
                "(idempotent re-run; preserves snapshots and warm.py)."
            )
        # Same version — `nb init` is idempotent by default.
        print(f"[notebook] already initialised at v{existing}; reasserting installs.",
              file=sys.stderr)
    nbd.mkdir(parents=True, exist_ok=True)
    (nbd / "version").write_text(f"{SKILL_VERSION}\n")
    return nbd


def _copy_template(name: str, dest: Path, *, overwrite: bool = False) -> None:
    src = TEMPLATES / name
    if not src.exists():
        return
    if dest.exists() and not overwrite:
        return
    shutil.copy2(src, dest)


def _append_to_file_if_missing(file: Path, marker: str, content: str) -> None:
    cur = file.read_text() if file.exists() else ""
    if marker in cur:
        return
    if cur and not cur.endswith("\n"):
        cur += "\n"
    file.write_text(cur + content)


# ---------------------------------------------------------------------------
# V1-B3: kernelspec auto-discovery + install.
# ---------------------------------------------------------------------------


def _resolve_project_venv(repo_root: Path) -> Path | None:
    cand = repo_root / ".venv" / "bin" / "python"
    if cand.exists():
        return cand
    return None


def _install_kernelspec(repo_root: Path, project: str, nbd: Path) -> str | None:
    """Register the project venv as a Jupyter kernelspec named `<project>-venv`.

    Writes the resolved name to `<nbd>/kernel_name` so `nb execute` resolves it.
    Returns the kernel name on success, None on skip.
    """
    py = _resolve_project_venv(repo_root)
    kn_file = nbd / "kernel_name"
    if py is None:
        if kn_file.exists():
            return kn_file.read_text().strip() or None
        print(f"[notebook] no `.venv/bin/python` at {repo_root}; "
              "kernel will fall back to system `python3` (may lack project deps).",
              file=sys.stderr)
        return None
    kernel_name = f"{project}-venv"
    display = f"Python ({kernel_name})"
    try:
        subprocess.run(
            [str(py), "-m", "ipykernel", "install", "--user",
             "--name", kernel_name, "--display-name", display],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"[notebook] ipykernel install failed: {e.stderr or e.stdout or e}\n"
              f"  Install ipykernel into the venv: {py} -m pip install ipykernel\n"
              "  Then re-run `nb init --migrate`.", file=sys.stderr)
        return None
    except FileNotFoundError:
        print(f"[notebook] {py} not found; skipping kernelspec install.", file=sys.stderr)
        return None
    kn_file.write_text(f"{kernel_name}\n")
    return kernel_name


# ---------------------------------------------------------------------------
# V1-H4: warm.py auto-detect.
# ---------------------------------------------------------------------------


_WARM_PRESETS = {
    "qiskit": (
        ("qiskit", "qiskit_aer"),
        '''try:
    import qiskit
    import qiskit_aer
    from qiskit_aer import AerSimulator
    _ = AerSimulator()  # forces backend instantiation, caches BLAS
    print(f"[warm] qiskit {qiskit.__version__} + Aer ready")
except Exception as e:
    print(f"[warm] qiskit failed: {e}")
''',
    ),
    "torch": (
        ("torch",),
        '''try:
    import torch
    print(f"[warm] torch {torch.__version__} cuda={torch.cuda.is_available()}")
except Exception as e:
    print(f"[warm] torch failed: {e}")
''',
    ),
    "scipy_sklearn": (
        ("scipy", "sklearn"),
        '''try:
    import scipy
    print(f"[warm] scipy {scipy.__version__}")
except Exception as e:
    print(f"[warm] scipy failed: {e}")
try:
    import sklearn
    print(f"[warm] sklearn {sklearn.__version__}")
except Exception as e:
    print(f"[warm] sklearn failed: {e}")
''',
    ),
}

_WARM_BASE = '''# <project>/.notebook/warm.py — kernel pre-import config (V1.1: auto-generated).
# Each import block wrapped in try/except — failures log but do not abort.

try:
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    print("[warm] numpy + matplotlib ready")
except Exception as e:
    print(f"[warm] numpy/matplotlib failed: {e}")

'''


def _detect_installed(py: Path) -> set[str]:
    if py is None:
        return set()
    try:
        out = subprocess.check_output(
            [str(py), "-m", "pip", "list", "--format=json"],
            text=True, stderr=subprocess.DEVNULL, timeout=15.0,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return set()
    try:
        installed = json.loads(out)
    except json.JSONDecodeError:
        return set()
    return {p["name"].lower().replace("-", "_") for p in installed}


def _generate_warm_py(repo_root: Path, nbd: Path) -> None:
    """V1-H4: detect heavy deps via pip list and emit warm.py with relevant
    blocks uncommented. Always writes (overwrites any prior auto-generated)."""
    py = _resolve_project_venv(repo_root)
    pkgs = _detect_installed(py) if py else set()
    body = _WARM_BASE
    for name, (markers, block) in _WARM_PRESETS.items():
        if any(m.lower().replace("-", "_") in pkgs for m in markers):
            body += f"# auto-detected: {name}\n{block}\n"
    dest = nbd / "warm.py"
    # Don't clobber a hand-edited warm.py from a prior version; only write if
    # the file is missing OR contains only the original V1 commented template
    # (signature: `# Uncomment the blocks your project needs`).
    if dest.exists():
        cur = dest.read_text()
        if "# Uncomment the blocks your project needs" not in cur \
                and "auto-generated" not in cur:
            print(f"[notebook] preserving hand-edited {dest}; "
                  "re-run `nb init --migrate --force` to regenerate.",
                  file=sys.stderr)
            return
    dest.write_text(body)


# ---------------------------------------------------------------------------
# V1-B4 + V1-L4: merge driver install + worktree marker.
# ---------------------------------------------------------------------------


def _install_merge_driver(repo_root: Path, nbd: Path) -> None:
    driver_script = TEMPLATES / "merge_regen.py"
    try:
        # V1-L4: ensure executable bit (WSL/NTFS sometimes loses it).
        driver_script.chmod(0o755)
    except OSError:
        pass
    try:
        subprocess.run(
            ["git", "-C", str(repo_root), "config", "merge.jupytext-regen.driver",
             f"python3 {driver_script} %A %O %B %P"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo_root), "config", "merge.jupytext-regen.name",
             "Drop ipynb conflict, regenerate from paired .py"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo_root), "config", "merge.jupytext-regen.recursive", "binary"],
            check=True, capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"[notebook] merge driver install failed: {e}", file=sys.stderr)
        return
    # V1-B4: per-worktree marker so `nb batch` can detect missing init.
    (nbd / ".merge-driver-installed").write_text(f"{SKILL_VERSION}\n")


def _install_pre_commit_hook(repo_root: Path) -> None:
    template = TEMPLATES / "pre-commit-reject-ipynb-only.sh"
    if not template.exists():
        return
    # V3-X1: in linked worktrees, `<worktree>/.git` is a FILE (gitdir pointer),
    # not a directory. Resolve the hooks dir via `git rev-parse --git-path
    # hooks` so we route through the canonical `.git/hooks/` regardless.
    try:
        hooks_dir = Path(subprocess.check_output(
            ["git", "-C", str(repo_root), "rev-parse", "--git-path", "hooks"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip())
        if not hooks_dir.is_absolute():
            hooks_dir = (repo_root / hooks_dir).resolve()
    except (subprocess.CalledProcessError, FileNotFoundError):
        hooks_dir = repo_root / ".git" / "hooks"  # legacy fallback
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-commit"
    content = template.read_text()
    _append_to_file_if_missing(hook, "# notebook-skill: reject ipynb without paired py", content)
    hook.chmod(0o755)


def _capture_jupytext_path(nbd: Path) -> None:
    """V3-UX4: persist absolute path of jupytext into `.notebook/jupytext_path`.

    Git's merge-driver and pre-commit-hook subshells do NOT inherit the user's
    PATH (no `.venv/bin`). `shutil.which("jupytext")` from `nb init`'s shell
    DOES see `.venv/bin`, so we capture the resolved absolute path here.
    Consumers (merge_regen.py, pre-commit-reject-ipynb-only.sh) read it back.
    """
    resolved = shutil.which("jupytext")
    if resolved is None:
        # Don't write a stale/empty file — let consumers fall back to PATH.
        print("[notebook] WARNING: jupytext not on PATH at init time; "
              ".notebook/jupytext_path NOT written. Merge driver and "
              "pre-commit hook may fail in subshells. Install jupytext "
              "and re-run `nb init --migrate`.", file=sys.stderr)
        return
    (nbd / "jupytext_path").write_text(f"{resolved}\n")


# ---------------------------------------------------------------------------
# V1-H3: runtime probe injection with loud warning.
# ---------------------------------------------------------------------------


def _ensure_runtime_probe(nb_path: Path, *, skip: bool) -> bool:
    """Return True if a probe was injected (cells shifted), False otherwise."""
    nb = nbformat.read(str(nb_path), as_version=4)
    probe_marker = "RUNTIME = "
    for cell in nb.cells[:3]:
        src = cell.source if isinstance(cell.source, str) else "".join(cell.source)
        if probe_marker in src:
            return False
    if skip:
        print(f"[notebook] skipping runtime probe injection (--no-probe-injection).",
              file=sys.stderr)
        return False
    probe_md = (TEMPLATES / "runtime_probe.md").read_text()
    new_cell = nbformat.v4.new_code_cell(
        source=probe_md, metadata={"tags": ["notebook-runtime-probe"]}
    )
    nb.cells.insert(0, new_cell)
    atomic_write_ipynb(nb_path, nb)
    return True


def _ensure_paired_py(nb_path: Path) -> None:
    py = nb_path.with_suffix(".py")
    if py.exists():
        return
    if not shutil.which("jupytext"):
        print(f"[notebook] WARNING: jupytext not on PATH — paired .py not created. "
              "Install jupytext>=1.16 and re-run init.", file=sys.stderr)
        return
    try:
        subprocess.run(
            ["jupytext", "--set-formats", "ipynb,py:percent", str(nb_path)],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"[notebook] jupytext --set-formats failed: {e.stderr}", file=sys.stderr)


# ---------------------------------------------------------------------------
# V1-H18: legacy-script deprecation hint.
# ---------------------------------------------------------------------------


def _legacy_scripts_warning(repo_root: Path, nb_path: Path) -> None:
    scripts_dir = nb_path.parent / "scripts"
    if not scripts_dir.exists() or not scripts_dir.is_dir():
        return
    legacy_re = re.compile(
        r"json\.(?:load|dump)\s*\(.*\.ipynb|nbformat\.write\s*\([^)]*open\("
    )
    matches = []
    for p in scripts_dir.glob("*.py"):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        if legacy_re.search(text):
            matches.append(p.name)
    if not matches:
        return
    print(
        f"\n[notebook] DEPRECATION HINT: found {len(matches)} legacy scaffolder(s) in "
        f"{scripts_dir}:\n  " + "\n  ".join(matches) +
        "\n  These use raw json/nbformat patterns and will be hard-blocked by the "
        "PreToolUse hook on next agent run.\n  Migrate to `nb batch <nb> --plan plan.yml` "
        "after the first AT pass; keep them in git history for the migration window.\n",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# V1-H7: .gitignore management.
# ---------------------------------------------------------------------------


def _update_gitignore(repo_root: Path) -> None:
    gi = repo_root / ".gitignore"
    snippet = (
        "\n# notebook-skill: per-notebook lock files + per-project snapshots\n"
        "*.ipynb.lock\n"
        ".notebook/snapshots/\n"
    )
    _append_to_file_if_missing(gi, "*.ipynb.lock", snippet)


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


def cmd_init(args) -> int:
    nb_path: Path = args.notebook.resolve()
    if not nb_path.exists():
        sys.exit(f"[notebook] not found: {nb_path}")
    _refuse_mnt_c(nb_path)
    repo_root = canonical_repo_root(nb_path.parent)
    _refuse_bare_repo(repo_root)

    # V1-L2: lock during init to prevent concurrent inits.
    with notebook_lock(nb_path, timeout=30.0):
        nbd = _ensure_notebook_dir(nb_path, args.force, getattr(args, "migrate", False))

        # warm.py auto-detect.
        _generate_warm_py(repo_root, nbd)

        # forbidden_imports defaults.
        _copy_template("forbidden_imports.txt", nbd / "forbidden_imports.txt")

        # marker file.
        (nbd / ".requirements.sha256").touch()

        # .gitattributes.
        ga = repo_root / ".gitattributes"
        _append_to_file_if_missing(
            ga, "merge=jupytext-regen",
            (TEMPLATES / "gitattributes-snippet").read_text(),
        )

        # .gitignore (V1-H7).
        _update_gitignore(repo_root)

        # .pre-commit-config.yaml — append a marker.
        pc = repo_root / ".pre-commit-config.yaml"
        if not pc.exists():
            shutil.copy2(TEMPLATES / "pre-commit-config.yaml", pc)
        else:
            _append_to_file_if_missing(
                pc, "# notebook-skill hooks",
                "\n# notebook-skill hooks: see "
                f"{TEMPLATES / 'pre-commit-config.yaml'} to merge.\n",
            )

        # Merge driver + per-worktree marker.
        _install_merge_driver(repo_root, nbd)

        # V3-UX4: capture absolute path of jupytext for subshells (merge driver
        # + pre-commit hook) that don't inherit `.venv/bin` in PATH.
        _capture_jupytext_path(nbd)

        # Pre-commit hook (rejection for ipynb-without-py).
        _install_pre_commit_hook(repo_root)

        # Paired .py.
        _ensure_paired_py(nb_path)

        # Kernelspec install (V1-B3).
        project = repo_root.name
        kernel_name = _install_kernelspec(repo_root, project, nbd)

        # Runtime probe — emits cell-shift warning if it actually injects.
        skip_probe = getattr(args, "no_probe_injection", False)
        injected = _ensure_runtime_probe(nb_path, skip=skip_probe)
        if injected:
            print(
                "\n[notebook] *** WARNING: runtime probe injected at cell index 0. ***\n"
                "  All existing cell indices shifted by +1. If you are about to author a\n"
                "  plan.yml with `at_position: N`, the positions are now stale. Address\n"
                "  cells by `cell_id` or `cell_tag` instead, OR re-derive positions after\n"
                "  this init. (Use `nb find <pat>` to locate cells by content.)\n",
                file=sys.stderr,
            )

        # Legacy scaffolders deprecation hint.
        _legacy_scripts_warning(repo_root, nb_path)

    print(f"[notebook] init done: {nb_path}")
    print(f"[notebook]   .notebook/        → {nbd}")
    print(f"[notebook]   .gitattributes    → {ga}")
    print(f"[notebook]   .gitignore        → {repo_root / '.gitignore'} (lock+snapshots)")
    print(f"[notebook]   .pre-commit       → {pc}")
    print(f"[notebook]   merge driver      → registered in .git/config "
          f"(per-worktree marker: {nbd / '.merge-driver-installed'})")
    print(f"[notebook]   pre-commit hook   → installed at .git/hooks/pre-commit")
    print(f"[notebook]   kernelspec        → {kernel_name or 'system python3 (NOT RECOMMENDED)'}")
    print(f"[notebook]   warm.py           → auto-detected blocks: {nbd / 'warm.py'}")
    print(f"[notebook] Next: `pip install -r {TEMPLATES / 'requirements-skill.txt'}`")
    return 0
