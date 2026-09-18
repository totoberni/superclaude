#!/usr/bin/env python3
"""
better_super_deps.py — unified dependency manifest CLI for superclaude.

Parses ~/.claude/dependencies.yml (schema: 1) and provides:
  --list          enumerate entries (optionally filtered by --type)
  --check         per-entry currency check (routes per dependency type)
  --record        append or update an entry (idempotent by (type, name))
  --export        emit requirements-format text (name==version lines) from python: section
                  --out PATH  write to file; omit to print to stdout
                  (alias: --sync → stdout, kept for backward compat)
  --pip-install   install all python: entries into ~/.claude/.venv via venv pip
                  --dry-run  print the pip command and exit without running

Network routing:
  python  → PyPI JSON API (https://pypi.org/pypi/<name>/json) — sandbox-allowlisted
  npm     → registry.npmjs.org — NOT sandbox-allowlisted → status: needs-network

Used by /better-super --new (--record self-registration) and --update (--check + --export).
Schema and CLI interface are stable; consuming skills reference this docstring as the contract.
"""

import argparse
import copy
import sys
import os
import urllib.request
import urllib.error
import json
from datetime import date
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    sys.exit("PyYAML not found: run `uv pip install --python <venv python> PyYAML`")

MANIFEST_PATH = Path.home() / ".claude" / "dependencies.yml"
# Interprete del venv: Scripts/python.exe su Windows, bin/python altrove.
if os.name == "nt":
    VENV_PYTHON = Path.home() / ".claude" / ".venv" / "Scripts" / "python.exe"
else:
    VENV_PYTHON = Path.home() / ".claude" / ".venv" / "bin" / "python"
PYPI_URL_TEMPLATE = "https://pypi.org/pypi/{name}/json"

# ---------------------------------------------------------------------------
# Manifest I/O
# ---------------------------------------------------------------------------

def load_manifest(path: Path = MANIFEST_PATH) -> dict:
    if not path.exists():
        sys.exit(f"Manifest not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or data.get("schema") != 1:
        sys.exit(f"Unsupported manifest schema in {path} (expected schema: 1)")
    # Ensure all type sections exist
    for section in ("python", "npm"):
        data.setdefault(section, [])
    return data


def save_manifest(data: dict, path: Path = MANIFEST_PATH) -> None:
    inline_comment = {
        "python": "# pip; pinned; --export → requirements-format / --pip-install → venv; currency via PyPI JSON API",
        "npm":    "# node; currency via registry.npmjs.org (external — not sandbox-allowlisted)",
    }
    with path.open("w", encoding="utf-8") as f:
        f.write(f"schema: {data['schema']}\n")
        f.write(f"updated: \"{data.get('updated', '')}\"\n\n")
        for section in ("python", "npm"):
            entries = data.get(section, [])
            comment = inline_comment[section]
            f.write(f"{section}:   {comment}\n")
            if entries:
                # dump each entry as a flow-style mapping on a single "  - {...}" line
                for entry in entries:
                    # yaml.dump(entry, default_flow_style=True) → "{k: v, ...}\n"
                    inline = yaml.dump(
                        entry,
                        default_flow_style=True,
                        sort_keys=False,
                        allow_unicode=True,
                        width=9999,
                    ).rstrip("\n")
                    f.write(f"  - {inline}\n")
            f.write("\n")

# ---------------------------------------------------------------------------
# --list
# ---------------------------------------------------------------------------

def cmd_list(args: argparse.Namespace, data: dict) -> None:
    types = [args.type] if args.type else ["python", "npm"]
    for t in types:
        entries = data.get(t, [])
        if not entries:
            continue
        print(f"\n=== {t} ({len(entries)} entries) ===")
        for e in entries:
            if t == "python":
                print(f"  {e.get('name','<missing>')}=={e.get('version','?')}  via={e.get('via','?')}  # {e.get('reason','')}")
            elif t == "npm":
                scope = e.get("scope")
                pkg = f"@{scope}/{e['name']}" if scope else e["name"]
                print(f"  {pkg}  install={e.get('install','?')}  version={e.get('version','?')}  via={e.get('via','?')}  # {e.get('reason','')}")

# ---------------------------------------------------------------------------
# --check  (routes per type)
# ---------------------------------------------------------------------------

def pypi_latest(name: str) -> tuple[str, str]:
    """Return (latest_version, status) where status is 'ok' or an error string."""
    url = PYPI_URL_TEMPLATE.format(name=name)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "better-super-deps/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read())
        return payload["info"]["version"], "ok"
    except urllib.error.HTTPError as exc:
        return "", f"http-{exc.code}"
    except urllib.error.URLError as exc:
        return "", f"url-error: {exc.reason}"
    except Exception as exc:  # noqa: BLE001
        return "", f"error: {exc}"


def check_python(entries: list) -> None:
    if not entries:
        return
    print(f"{'name':<20} {'current':<15} {'latest':<15} {'status'}")
    print("-" * 65)
    for e in entries:
        name = e["name"]
        current = e.get("version", "?")
        latest, err = pypi_latest(name)
        if err != "ok":
            status = f"unreachable ({err})"
            latest = "?"
        elif latest == current:
            status = "current"
        else:
            status = "outdated"
        print(f"{name:<20} {current:<15} {latest:<15} {status}")


def check_npm(entries: list) -> None:
    # registry.npmjs.org is NOT in the sandbox allowlist → needs-network
    print(f"{'name':<30} {'current':<15} {'latest':<15} {'status'}")
    print("-" * 75)
    for e in entries:
        scope = e.get("scope")
        pkg = f"@{scope}/{e['name']}" if scope else e["name"]
        current = e.get("version", "?")
        # Real check: GET https://registry.npmjs.org/<pkg> → .dist-tags.latest
        # Not sandbox-allowlisted — mark gracefully without crashing.
        print(f"{pkg:<30} {current:<15} {'?':<15} needs-network")


def cmd_check(args: argparse.Namespace, data: dict) -> None:
    types = [args.type] if args.type else ["python", "npm"]
    for t in types:
        entries = data.get(t, [])
        if not entries:
            continue
        print(f"\n=== {t} ===")
        if t == "python":
            check_python(entries)
        elif t == "npm":
            check_npm(entries)

# ---------------------------------------------------------------------------
# --record  (append or update, idempotent by (type, name))
# ---------------------------------------------------------------------------

def cmd_record(args: argparse.Namespace, data: dict) -> None:
    dep_type = args.type
    name = args.name
    version = args.version
    via = args.via
    reason = args.reason

    if dep_type in ("python", "npm") and not version:
        print(f"error: --version is required for --type {dep_type}", file=sys.stderr)
        sys.exit(1)

    if dep_type == "python":
        entry = {"name": name, "version": version, "via": via, "reason": reason}
    elif dep_type == "npm":
        entry = {
            "name": name,
            "scope": args.scope,
            "install": args.install or "npx-latest",
            "version": version,
            "via": via,
            "reason": reason,
        }
    else:
        sys.exit(f"Unknown type: {dep_type}")

    entries = data.setdefault(dep_type, [])
    # Idempotent: update if (type, name) already exists
    for i, existing in enumerate(entries):
        if existing.get("name") == name:
            entries[i] = entry
            data["updated"] = date.today().isoformat()
            save_manifest(data)
            print(f"Updated {dep_type}/{name} in manifest.")
            return

    entries.append(entry)
    data["updated"] = date.today().isoformat()
    save_manifest(data)
    print(f"Recorded {dep_type}/{name} in manifest.")

# ---------------------------------------------------------------------------
# --export  (emit requirements-format text from python: section)
# --sync    (hidden alias → --export to stdout, for backward compat)
# ---------------------------------------------------------------------------

def _requirements_lines(python_entries: list) -> list[str]:
    """Build requirements-format lines from the python: section."""
    lines: list[str] = []
    for e in python_entries:
        name = e["name"]
        version = e.get("version", "")
        if version:
            lines.append(f"{name}=={version}")
        else:
            lines.append(name)
    return lines


def cmd_export(args: argparse.Namespace, data: dict) -> None:
    python_entries = data.get("python", [])
    lines = _requirements_lines(python_entries)
    out_path = Path(args.out) if getattr(args, "out", None) else None

    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"Wrote {len(python_entries)} entries to {out_path}")
    else:
        print("\n".join(lines))


# ---------------------------------------------------------------------------
# --pip-install  (install python: entries into venv)
# ---------------------------------------------------------------------------

def cmd_pip_install(args: argparse.Namespace, data: dict) -> None:
    import shutil
    import subprocess

    python_entries = data.get("python", [])
    specs = _requirements_lines(python_entries)

    if not VENV_PYTHON.exists():
        print(f"error: venv interpreter not found: {VENV_PYTHON}", file=sys.stderr)
        sys.exit(1)
    # Il venv e' creato da uv e puo' non contenere pip: si preferisce
    # `uv pip install --python <venv python>`, con ripiego su `<venv python> -m pip`.
    uv = shutil.which("uv")
    if uv:
        pip_prefix = [uv, "pip", "install", "--python", str(VENV_PYTHON)]
    else:
        pip_prefix = [str(VENV_PYTHON), "-m", "pip", "install"]

    cmd = pip_prefix + specs
    if args.dry_run:
        print(" ".join(cmd))
        sys.exit(0)

    result = subprocess.run(cmd)
    sys.exit(result.returncode)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    # Console Windows (cp1252): forza UTF-8 per i caratteri non ASCII in uscita.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    # argparse subparsers keyed by strings starting with "--" need special handling.
    # We parse argv manually: first positional after the script name is the command.
    # To keep argparse happy, remap bare "--list"/"--check"/"--record"/"--sync"
    # to subcommand names without the leading dashes.
    args_in = sys.argv[1:]
    # Remap the first "--X" that matches a known command to its bare name.
    # "--sync" is a hidden alias for "export" (stdout, no --out).
    commands = {"--list", "--check", "--record", "--sync", "--export", "--pip-install"}
    remapped = []
    found_cmd = False
    for tok in args_in:
        if not found_cmd and tok in commands:
            bare = tok.lstrip("-")
            if bare == "sync":
                bare = "export"  # hidden alias → export to stdout
            elif bare == "pip-install":
                bare = "pip_install"  # argparse subcommand name (no hyphen)
            remapped.append(bare)
            found_cmd = True
        else:
            remapped.append(tok)

    # Re-parse sub-commands without leading dashes
    p = argparse.ArgumentParser(
        prog="better-super-deps",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--manifest", default=str(MANIFEST_PATH))

    sub = p.add_subparsers(dest="command")

    ls = sub.add_parser("list")
    ls.add_argument("--type", choices=["python", "npm"])

    chk = sub.add_parser("check")
    chk.add_argument("--type", choices=["python", "npm"])

    rec = sub.add_parser("record")
    rec.add_argument("--type", required=True, choices=["python", "npm"])
    rec.add_argument("--name", required=True)
    rec.add_argument("--version", required=False, default=None,
                     help="Package version (required for python/npm)")
    rec.add_argument("--via", required=True)
    rec.add_argument("--reason", required=True)
    rec.add_argument("--scope", default=None)
    rec.add_argument("--install", default=None)

    exp = sub.add_parser("export")
    exp.add_argument("--out", default=None,
                     help="Write to this file path; omit to print to stdout")

    pip_inst = sub.add_parser("pip_install")
    pip_inst.add_argument("--dry-run", dest="dry_run", action="store_true",
                          help="Print the pip command and exit without running")

    args = p.parse_args(remapped)

    if not args.command:
        p.print_help()
        sys.exit(1)

    manifest_path = Path(args.manifest)
    data = load_manifest(manifest_path)

    if args.command == "list":
        cmd_list(args, data)
    elif args.command == "check":
        cmd_check(args, data)
    elif args.command == "record":
        cmd_record(args, data)
    elif args.command == "export":
        cmd_export(args, data)
    elif args.command == "pip_install":
        cmd_pip_install(args, data)


if __name__ == "__main__":
    main()
