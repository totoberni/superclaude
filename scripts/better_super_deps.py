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
  docker  → docker manifest inspect <image>:<tag> — NOT sandbox-allowlisted → status: needs-network

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
    sys.exit("PyYAML not found — run: ~/.claude/.venv/bin/pip install PyYAML")

MANIFEST_PATH = Path.home() / ".claude" / "dependencies.yml"
VENV_PIP = Path.home() / ".claude" / ".venv" / "bin" / "pip"
PYPI_URL_TEMPLATE = "https://pypi.org/pypi/{name}/json"

# ---------------------------------------------------------------------------
# Manifest I/O
# ---------------------------------------------------------------------------

def load_manifest(path: Path = MANIFEST_PATH) -> dict:
    if not path.exists():
        sys.exit(f"Manifest not found: {path}")
    with path.open("r") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or data.get("schema") != 1:
        sys.exit(f"Unsupported manifest schema in {path} (expected schema: 1)")
    # Ensure all three type sections exist
    for section in ("python", "npm", "docker"):
        data.setdefault(section, [])
    return data


def save_manifest(data: dict, path: Path = MANIFEST_PATH) -> None:
    inline_comment = {
        "python": "# pip; pinned; --export → requirements-format / --pip-install → venv; currency via PyPI JSON API",
        "npm":    "# node; currency via registry.npmjs.org (external — not sandbox-allowlisted)",
        "docker": "# compose; currency via registry digest (external — checked via docker manifest inspect)",
    }
    with path.open("w") as f:
        f.write(f"schema: {data['schema']}\n")
        f.write(f"updated: \"{data.get('updated', '')}\"\n\n")
        for section in ("python", "npm", "docker"):
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
    types = [args.type] if args.type else ["python", "npm", "docker"]
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
            elif t == "docker":
                print(f"  {e['name']}  image={e.get('image','?')}:{e.get('tag','?')}  compose={e.get('compose','?')}  endpoint={e.get('endpoint','null')}  via={e.get('via','?')}")

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


def check_docker(entries: list) -> None:
    # docker manifest inspect <image>:<tag> requires docker daemon + network
    # Not accessible in sandbox — mark gracefully.
    # Real check: docker manifest inspect <image>:<tag> | jq '.[0].Digest'
    print(f"{'name':<20} {'image:tag':<40} {'status'}")
    print("-" * 75)
    for e in entries:
        name = e["name"]
        image_tag = f"{e.get('image','?')}:{e.get('tag','?')}"
        print(f"{name:<20} {image_tag:<40} needs-network")


def cmd_check(args: argparse.Namespace, data: dict) -> None:
    types = [args.type] if args.type else ["python", "npm", "docker"]
    for t in types:
        entries = data.get(t, [])
        if not entries:
            continue
        print(f"\n=== {t} ===")
        if t == "python":
            check_python(entries)
        elif t == "npm":
            check_npm(entries)
        elif t == "docker":
            check_docker(entries)

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
    elif dep_type == "docker":
        entry = {
            "name": name,
            "image": args.image or "",
            "tag": args.tag or "latest",
            "digest": None,
            "compose": args.compose or "",
            "endpoint": args.endpoint,
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
        out_path.write_text("\n".join(lines) + "\n")
        print(f"Wrote {len(python_entries)} entries to {out_path}")
    else:
        print("\n".join(lines))


# ---------------------------------------------------------------------------
# --pip-install  (install python: entries into venv)
# ---------------------------------------------------------------------------

def cmd_pip_install(args: argparse.Namespace, data: dict) -> None:
    import subprocess

    python_entries = data.get("python", [])
    specs = _requirements_lines(python_entries)

    if VENV_PIP.exists():
        pip_prefix = [str(VENV_PIP)]
    else:
        # Fallback: python3 -m pip. Keep the argv elements SEPARATE — a single
        # "python3 -m pip" string as argv[0] would fail (space in the executable).
        import shutil
        python3 = shutil.which("python3")
        if python3 is None:
            print("error: ~/.claude/.venv/bin/pip not found and python3 not in PATH", file=sys.stderr)
            sys.exit(1)
        pip_prefix = [python3, "-m", "pip"]
        print(f"warning: {VENV_PIP} not found — falling back to {' '.join(pip_prefix)}", file=sys.stderr)

    cmd = pip_prefix + ["install"] + specs
    if args.dry_run:
        print(" ".join(cmd))
        sys.exit(0)

    result = subprocess.run(cmd)
    sys.exit(result.returncode)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
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
    ls.add_argument("--type", choices=["python", "npm", "docker"])

    chk = sub.add_parser("check")
    chk.add_argument("--type", choices=["python", "npm", "docker"])

    rec = sub.add_parser("record")
    rec.add_argument("--type", required=True, choices=["python", "npm", "docker"])
    rec.add_argument("--name", required=True)
    rec.add_argument("--version", required=False, default=None,
                     help="Package version (required for python/npm; omit for docker)")
    rec.add_argument("--via", required=True)
    rec.add_argument("--reason", required=True)
    rec.add_argument("--scope", default=None)
    rec.add_argument("--install", default=None)
    rec.add_argument("--image", default=None)
    rec.add_argument("--tag", default=None)
    rec.add_argument("--compose", default=None)
    rec.add_argument("--endpoint", default=None)

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
