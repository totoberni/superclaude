"""Profile-aware settings applier: writes hooks, shared permission rules and the profile template.

Run by the owner, never by an agent against a real home (except --dry-run).
Target file: sc_profile.settings_paths(profile)[0]. The entries the applier wrote last are
recorded in a sidecar (<settings>.applier-owned.json) so later runs can retract them.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
import sc_profile  # noqa: E402

HERE = Path(__file__).resolve().parent
RULES_FILE = HERE / "permission-rules.json"
TEMPLATES: dict[str, Path] = {
    "deepseek": HERE / "deepseek.base.json",
    "superclaude": HERE / "superclaude.base.json",
}
# Modelli facoltativi: se il file manca il profilo usa un modello vuoto
OPTIONAL_TEMPLATES = frozenset({"superclaude"})
PLACEHOLDERS = ("{{PYTHON}}", "{{DISPATCH}}")
# Caratteri con significato per il matcher delle regole (jolly, escape, separatori, parentesi della regola)
HOME_USER_FORBIDDEN = frozenset("*\\/()")
# Marcatore che identifica una sezione 'hooks' scritta da superclaude
OUR_HOOKS_MARKER = "hooks/dispatch.py"
BACKUP_INFIX = ".bak-"
PRE_REVERT_INFIX = ".pre-revert-"
OWNED_SUFFIX = ".applier-owned.json"
STAMP_FORMAT = "%Y%m%d-%H%M%S-%f"
# Solo i backup con il timbro dell'applicatore (microsecondi inclusi) sono candidati per --revert
STAMP_RE = re.compile(r"\d{8}-\d{6}-\d{6}")

TreePath = tuple[str, ...]
Owned = dict[TreePath, Any]


class ApplyError(RuntimeError):
    """Precondition failure: nothing has been written."""


def _posix(path: Path) -> str:
    return str(path).replace("\\", "/")


def base_python() -> Path:
    # Python 3.12 di base (non il venv): gli hook lo avviano con -S, niente site-packages
    return Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Python" / "Python312" / "python.exe"


def dispatch_script() -> Path:
    return sc_profile.code_home() / "hooks" / "dispatch.py"


def hooks_source() -> Path:
    return sc_profile.code_home() / "hooks" / "hooks.json"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_object(path: Path, what: str) -> dict[str, Any]:
    """JSON object from disk; a malformed file becomes a clean ApplyError (nothing written)."""
    try:
        data = read_json(path)
    except ValueError as exc:
        raise ApplyError(f"{what} {path} is not valid JSON, nothing written: {exc}") from exc
    if not isinstance(data, dict):
        raise ApplyError(f"{what} {path} is not a JSON object, nothing written")
    return data


def render_hooks(drop_stop: bool = False) -> dict[str, Any]:
    """hooks.json with placeholders replaced (textual replace on the raw text, then parse)."""
    py, dsp, src = base_python(), dispatch_script(), hooks_source()
    for need in (py, dsp, src):
        if not need.exists():
            raise ApplyError(f"missing prerequisite: {need}")
    raw = src.read_text(encoding="utf-8-sig")
    for token in PLACEHOLDERS:
        if token not in raw:
            raise ApplyError(f"hooks.json lacks placeholder {token}")
    hooks = json.loads(raw.replace("{{PYTHON}}", _posix(py)).replace("{{DISPATCH}}", _posix(dsp)))
    hooks.pop("_comment", None)  # commento della sorgente, non della configurazione
    if drop_stop:
        hooks.pop("Stop", None)
    _check_exec_form(hooks)
    return hooks


def _check_exec_form(hooks: dict[str, Any]) -> None:
    # In forma exec Windows non passa dalla shell: 'command' deve essere un vero .exe esistente
    for event, entries in hooks.items():
        for entry in entries:
            for hook in entry.get("hooks", []):
                if not hook.get("args"):
                    raise ApplyError(f"{event}: shell-form entry (missing 'args')")
                command = str(hook.get("command", ""))
                if not command.lower().endswith(".exe"):
                    raise ApplyError(f"{event}: 'command' is not an .exe: {command}")
                if not Path(command).exists():
                    raise ApplyError(f"{event}: command not found: {command}")


def checked_home_user(name: str) -> str:
    # Il nome finisce dentro le regole native: un carattere speciale per il matcher ne cambierebbe il senso
    if not name or any(ch in name for ch in HOME_USER_FORBIDDEN):
        raise ApplyError(f"home user name unusable in permission rules: {name!r}")
    return name


def template_values() -> dict[str, str]:
    return {"{{HOME}}": _posix(Path.home()), "{{DS_HOME}}": _posix(sc_profile.data_home("deepseek")),
            "{{HOME_USER}}": checked_home_user(Path.home().name)}


def substitute(node: Any, values: dict[str, str]) -> Any:
    if isinstance(node, dict):
        return {key: substitute(value, values) for key, value in node.items()}
    if isinstance(node, list):
        return [substitute(value, values) for value in node]
    if isinstance(node, str):
        for token, value in values.items():
            node = node.replace(token, value)
        if "{{" in node:
            raise ApplyError(f"template string has an unknown placeholder: {node[:80]}")
    return node


def load_template(profile: str) -> dict[str, Any]:
    path = TEMPLATES.get(profile)
    if path is None or (profile in OPTIONAL_TEMPLATES and not path.exists()):
        return {}
    if not path.exists():
        raise ApplyError(f"missing template: {path}")
    template = read_object(path, "template")
    template.pop("_comment", None)
    return substitute(template, template_values())


def desired_settings(profile: str) -> dict[str, Any]:
    """Everything the applier owns except 'hooks': profile template plus shared rules."""
    desired = load_template(profile)
    rules = substitute(read_object(RULES_FILE, "rules file"), template_values())
    perms = desired.setdefault("permissions", {})
    for kind in ("deny", "ask"):
        perms[kind] = union(perms.get(kind, []), rules.get(kind, []))
    perms["disableBypassPermissionsMode"] = "disable"
    return desired


def union(current: list[Any], wanted: list[Any]) -> list[Any]:
    merged: list[Any] = []
    for item in [*current, *wanted]:
        if item not in merged:
            merged.append(item)
    return merged


def flatten(tree: dict[str, Any], prefix: TreePath = ()) -> Owned:
    """Ordered path -> value; lists and non-dict values are leaves, dicts are walked (empty ones own nothing)."""
    out: Owned = {}
    for key, value in tree.items():
        path = (*prefix, key)
        if isinstance(value, dict):
            out.update(flatten(value, path))
        else:
            out[path] = value
    return out


_MISSING = object()


def get_path(tree: dict[str, Any], path: TreePath) -> Any:
    node: Any = tree
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return _MISSING
        node = node[key]
    return node


def set_path(tree: dict[str, Any], path: TreePath, value: Any) -> None:
    node = tree
    for key in path[:-1]:
        if not isinstance(node.get(key), dict):
            node[key] = {}
        node = node[key]
    node[path[-1]] = value


def delete_path(tree: dict[str, Any], path: TreePath) -> None:
    # Rimuove la foglia e poi i dizionari rimasti vuoti a causa della rimozione
    for depth in range(len(path), 0, -1):
        parent = get_path(tree, path[: depth - 1])
        if not isinstance(parent, dict) or path[depth - 1] not in parent:
            return
        if depth < len(path) and parent[path[depth - 1]]:
            return
        del parent[path[depth - 1]]


def merge_entries(current: Any, wanted: list[Any], owned: list[Any]) -> tuple[list[Any], list[Any]]:
    """Drop previously-owned entries no longer wanted, keep the rest in place, append the new ones."""
    existing = union(current if isinstance(current, list) else [], [])
    kept = [item for item in existing if item in wanted or item not in owned]
    added = [item for item in wanted if item not in kept]
    owned_now = [item for item in wanted if item in owned or item in added]
    return kept + added, owned_now


def reconcile(current: dict[str, Any], desired: dict[str, Any], owned: Owned) -> tuple[dict[str, Any], Owned]:
    """New settings plus the new ownership record.

    Entries the owner added are kept, with one exception: on a first run without sidecar an owner
    entry equal to a desired rule is adopted (see initial_owned) and can later be retracted.
    """
    result = copy.deepcopy(current)
    want = flatten(desired)
    new_owned: Owned = {}
    for path, value in want.items():
        if isinstance(value, list):
            before = owned.get(path)
            owned_entries = before if isinstance(before, list) else []
            merged, new_owned[path] = merge_entries(get_path(result, path), value, owned_entries)
            set_path(result, path, merged)
        else:
            set_path(result, path, copy.deepcopy(value))
            new_owned[path] = value
    for path, value in owned.items():
        if path in want:
            continue
        if isinstance(value, list):
            present = get_path(result, path)
            if isinstance(present, list):
                set_path(result, path, [item for item in present if item not in value])
        elif get_path(result, path) == value:
            delete_path(result, path)  # valore cambiato a mano dal proprietario: resta
    return result, new_owned


def initial_owned(current: dict[str, Any], desired: dict[str, Any]) -> Owned:
    """First run without sidecar: what already equals the templates counts as applier-owned."""
    owned: Owned = {}
    for path, value in flatten(desired).items():
        present = get_path(current, path)
        if isinstance(value, list):
            owned[path] = [item for item in value if isinstance(present, list) and item in present]
        elif present == value:
            owned[path] = value
    return owned


def owned_path(target: Path) -> Path:
    return target.with_name(target.name + OWNED_SUFFIX)


def load_owned(target: Path) -> Owned | None:
    """Ownership record of the last run; None when the sidecar does not exist yet."""
    sidecar = owned_path(target)
    if not sidecar.exists():
        return None
    entries = read_object(sidecar, "ownership sidecar").get("entries")
    try:
        return {tuple(item["path"]): item["value"] for item in entries}
    except (KeyError, TypeError) as exc:
        raise ApplyError(f"ownership sidecar {sidecar} is malformed, nothing written: {exc!r}") from exc


def serialize_owned(owned: Owned) -> str:
    entries = [{"path": list(path), "value": value} for path, value in owned.items()]
    return serialize({"_comment": "Written by apply_settings.py: entries it owns. Do not edit.",
                      "version": 1, "entries": entries})


def apply_hooks(settings: dict[str, Any], hooks: dict[str, Any], force: bool) -> None:
    if "hooks" in settings and OUR_HOOKS_MARKER not in json.dumps(settings["hooks"]) and not force:
        raise ApplyError("settings.json has an unrecognised 'hooks' section: review it, then rerun with --force")
    settings["hooks"] = hooks


def build_settings(profile: str, current: dict[str, Any], owned: Owned | None = None, *,
                   force: bool = False, drop_stop: bool = False) -> tuple[dict[str, Any], Owned]:
    desired = desired_settings(profile)
    previous = initial_owned(current, desired) if owned is None else owned
    result, new_owned = reconcile(current, desired, previous)
    apply_hooks(result, render_hooks(drop_stop), force)
    return result, new_owned


def serialize(settings: dict[str, Any]) -> str:
    return json.dumps(settings, indent=2, ensure_ascii=False) + "\n"


def target_path(profile: str) -> Path:
    return sc_profile.settings_paths(profile)[0]


def describe(label: str, before: Any, after: Any, depth: int = 0) -> list[str]:
    """Key-level change lines; values are never printed (only string list entries, i.e. rules)."""
    if before == after:
        return []
    # Una lista nuova o rimossa si confronta con una lista vuota, cosi' le regole compaiono una per una
    if before is _MISSING and isinstance(after, list):
        before = []
    if after is _MISSING and isinstance(before, list):
        after = []
    if before is _MISSING:
        return [f"+ {label}"]
    if after is _MISSING:
        return [f"- {label}"]
    if isinstance(before, dict) and isinstance(after, dict) and depth < 2:
        lines: list[str] = []
        for key in union(list(before), list(after)):
            lines += describe(f"{label}.{key}", before.get(key, _MISSING), after.get(key, _MISSING), depth + 1)
        return lines
    if isinstance(before, list) and isinstance(after, list):
        added = [item for item in after if item not in before]
        removed = [item for item in before if item not in after]
        lines = [f"~ {label}: +{len(added)} -{len(removed)}" + ("" if added or removed else " (reordered)")]
        lines += [f"    + {text}" for item in added for text in describe_item(item)]
        return lines + [f"    - {text}" for item in removed for text in describe_item(item)]
    return [f"~ {label}"]


def describe_item(item: Any) -> list[str]:
    """Printable form of a list entry: rules as-is, hook entries as matcher plus command and args."""
    if isinstance(item, str):
        return [item]
    if isinstance(item, dict) and isinstance(item.get("hooks"), list):
        matcher = item.get("matcher", "*")
        return [f"hook matcher={matcher}: {_hook_command(hook)}" for hook in item["hooks"] if isinstance(hook, dict)]
    return []


def _hook_command(hook: dict[str, Any]) -> str:
    # Solo comando e argomenti: le variabili d'ambiente dell'hook non vengono mai stampate
    args = hook.get("args")
    return " ".join([str(hook.get("command", "")), *(str(a) for a in args)] if isinstance(args, list)
                    else [str(hook.get("command", ""))])


def summarize(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for key in union(list(before), list(after)):
        lines += describe(key, before.get(key, _MISSING), after.get(key, _MISSING))
    return lines or ["(no changes)"]


def backups(target: Path) -> list[Path]:
    # Il timbro a larghezza fissa rende l'ordine alfabetico uguale a quello cronologico
    prefix = target.name + BACKUP_INFIX
    found = [p for p in target.parent.glob(prefix + "*") if STAMP_RE.fullmatch(p.name[len(prefix):])]
    return sorted(found, key=lambda p: p.name)


def stamp_now() -> str:
    return datetime.now().strftime(STAMP_FORMAT)


def copy_aside(path: Path, infix: str, stamp: str) -> Path | None:
    if not path.exists():
        return None
    dest = path.with_name(path.name + infix + stamp)
    atomic_write_bytes(dest, path.read_bytes())
    return dest


def make_backup(target: Path, infix: str = BACKUP_INFIX) -> Path:
    """Backup of settings plus sidecar under the same stamp, so --revert restores both together."""
    stamp = stamp_now()
    copy_aside(owned_path(target), infix, stamp)
    dest = copy_aside(target, infix, stamp)
    assert dest is not None
    return dest


def atomic_write_bytes(path: Path, data: bytes) -> None:
    # File temporaneo nella stessa cartella, poi os.replace: mai un file a meta'
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_text_lf(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def revert_preview(target: Path) -> tuple[Path, list[str]]:
    found = backups(target)
    if not found:
        raise ApplyError(f"no applier backup found next to {target}")
    last = found[-1]
    try:
        restored = read_json(last)  # un backup troncato renderebbe settings.json illeggibile
    except (ValueError, OSError) as exc:
        raise ApplyError(f"backup {last.name} is not valid JSON, revert aborted: {exc}") from exc
    try:
        current = read_json(target) if target.exists() else {}
    except ValueError:
        return last, ["(current file is not valid JSON: whole file replaced)"]
    if not isinstance(current, dict) or not isinstance(restored, dict):
        return last, ["(non-object JSON: whole file replaced)"]
    return last, summarize(current, restored)


def revert(target: Path, yes: bool) -> str:
    last, lines = revert_preview(target)
    head = f"revert {target} <- {last.name}"
    if not yes:
        return "\n".join([head + " (preview, nothing written)", *lines, "rerun with --yes to restore"])
    pre = make_backup(target, PRE_REVERT_INFIX).name if target.exists() else "none"
    atomic_write_bytes(target, last.read_bytes())
    sidecar_bak = last.with_name(owned_path(target).name + BACKUP_INFIX + last.name.rsplit(BACKUP_INFIX, 1)[1])
    if sidecar_bak.exists():
        atomic_write_bytes(owned_path(target), sidecar_bak.read_bytes())
    else:
        owned_path(target).unlink(missing_ok=True)  # backup anteriore al registro: si riparte da zero
    return "\n".join([head + f" (done; current saved as {pre})", *lines])


def plan(args: argparse.Namespace, target: Path) -> tuple[dict[str, Any], dict[str, Any], Owned, list[str]]:
    current = read_object(target, "settings file") if target.exists() else {}
    owned = load_owned(target)
    result, new_owned = build_settings(args.profile, current, owned, force=args.force, drop_stop=args.drop_stop)
    adopted = describe_adopted(initial_owned(current, desired_settings(args.profile))) if owned is None else []
    return current, result, new_owned, adopted


def describe_adopted(adopted: Owned) -> list[str]:
    """Summary of the owner entries that equal a desired value on a first run (now applier-owned)."""
    entries = [(".".join(path), text) for path, value in adopted.items()
               for text in (describe_item_list(value) if isinstance(value, list) else [None])]
    if not entries:
        return []
    return [f"adopted {len(entries)} existing entries"] + [
        f"    = {label}" + (f": {text}" if text else "") for label, text in entries]


def describe_item_list(values: list[Any]) -> list[str]:
    return [text for item in values for text in describe_item(item)]


def change_summary(current: dict[str, Any], result: dict[str, Any], adopted: list[str]) -> list[str]:
    return summarize(current, result) + adopted


def dry_run_report(target: Path, current: dict[str, Any], result: dict[str, Any], adopted: list[str],
                   full: bool) -> str:
    if full:
        return serialize(result)
    sidecar = "present" if owned_path(target).exists() else "absent (first run: template-equal entries owned)"
    head = [f"dry run: {target} (nothing written)", f"ownership sidecar: {sidecar}"]
    return "\n".join(head + change_summary(current, result, adopted)) + "\n"


def run(args: argparse.Namespace) -> str:
    target = target_path(args.profile)
    if args.revert:
        return revert(target, args.yes)
    current, result, new_owned, adopted = plan(args, target)
    if args.dry_run:
        return dry_run_report(target, current, result, adopted, args.full)
    text, owned_text = serialize(result), serialize_owned(new_owned)
    sidecar = owned_path(target)
    same_owned = sidecar.exists() and sidecar.read_text(encoding="utf-8-sig") == owned_text
    if target.exists() and target.read_text(encoding="utf-8-sig") == text:
        if not same_owned:
            write_text_lf(sidecar, owned_text)
        return f"{target}: already up to date, nothing written"
    note = f" (backup: {make_backup(target).name})" if target.exists() else ""
    # Prima il registro: se la scrittura del file fallisse, rimuovere voci assenti non costa nulla
    write_text_lf(sidecar, owned_text)
    write_text_lf(target, text)
    head = f"{target}: written{note}. Restart Claude Code; undo with --revert --yes"
    return "\n".join([head, *change_summary(current, result, adopted)])


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=sorted(sc_profile.profiles()))
    parser.add_argument("--dry-run", action="store_true", help="print a key-level summary, write nothing")
    parser.add_argument("--full", action="store_true", help="with --dry-run: print the full resulting JSON")
    parser.add_argument("--revert", action="store_true", help="preview restoring the latest applier backup")
    parser.add_argument("--yes", action="store_true", help="with --revert: actually restore")
    parser.add_argument("--force", action="store_true", help="replace an unrecognised 'hooks' section")
    parser.add_argument("--drop-stop", action="store_true", help="omit the Stop hook registration")
    args = parser.parse_args(argv)
    if args.full and not args.dry_run:
        parser.error("--full requires --dry-run")
    if args.yes and not args.revert:
        parser.error("--yes requires --revert")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        out = run(args)
    except ApplyError as exc:
        print(f"apply_settings: {exc}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(out.encode("utf-8") if out.endswith("\n") else (out + "\n").encode("utf-8"))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
