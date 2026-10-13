"""Matcher-port test for the native permission rules (permission-rules.json and profile templates).

The matcher reproduces Claude Code's Bash/PowerShell rule matching. Documented in permissions.md:
anchored match on each subcommand, '*' = any text, a trailing ' *' that is the only wildcard also
matches the bare command, compound commands split on the documented separators, wrappers and leading
assignments stripped, PowerShell case-insensitive with Remove-Item aliases canonicalised.
NOT documented in permissions.md (r9): backslash-star = literal star, double backslash = literal
backslash; source is the CLI source reading (claude.exe 2.1.293, rule matcher function) in the pass-1
review, plans/deepseek/hostile-review-2.md, run R3.
Every destructive command below is test data inside a string.
"""
from __future__ import annotations

import functools
import json
import re
from pathlib import Path

import pytest

SETTINGS = Path(__file__).resolve().parents[1] / "settings"
RULES_FILE = SETTINGS / "permission-rules.json"
DS_FILE = SETTINGS / "deepseek.base.json"
SC_FILE = SETTINGS / "superclaude.base.json"
ALL_FILES = (RULES_FILE, DS_FILE, SC_FILE)


# Segnaposto di template e regole resi con valori fissi di prova (l'applicatore li risolve a runtime)
TEMPLATE_VALUES = {"{{HOME}}": "C:/Users/x", "{{DS_HOME}}": "C:/Users/x/.claude-deepseek",
                   "{{HOME_USER}}": "x"}


def load(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    for token, value in TEMPLATE_VALUES.items():
        text = text.replace(token, value)
    return json.loads(text)


RULES, DS, SC = load(RULES_FILE), load(DS_FILE), load(SC_FILE)
RULE_RE = re.compile(r"^(Bash|PowerShell)\((.*)\)$", re.S)
BASH_OPS = ("&&", "||", "|&", ";", "|", "&", "\n")
PS_OPS = ("&&", "||", ";", "|", "\n")
WRAPPERS = ("time", "nohup", "command", "builtin", "noglob")
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*\s+")
PS_REMOVE_ALIASES = {"rm", "del", "rd", "ri", "rmdir", "erase"}


def parse(rule: str) -> tuple[str, str] | None:
    m = RULE_RE.match(rule)
    return (m.group(1), m.group(2)) if m else None


def _body(spec: str) -> tuple[str, int]:
    parts, i, wildcards = [], 0, 0
    while i < len(spec):
        ch = spec[i]
        if ch == "\\" and spec[i + 1:i + 2] in ("\\", "*"):
            parts.append(re.escape(spec[i + 1]))
            i += 2
            continue
        if ch == "*":
            parts.append(".*")
            wildcards += 1
        else:
            parts.append(re.escape(ch))
        i += 1
    return "".join(parts), wildcards


@functools.lru_cache(maxsize=None)
def pattern_regex(spec: str, ps: bool) -> re.Pattern[str]:
    body, wildcards = _body(spec)
    if wildcards == 1 and spec.endswith(" *"):
        # un '*' finale preceduto da spazio, unico jolly, copre anche il comando nudo
        body = _body(spec[:-2])[0] + "(?: .*)?"
    return re.compile(f"^{body}$", re.S | (re.I if ps else 0))


def _op_at(cmd: str, i: int, ops: tuple[str, ...]) -> str | None:
    for op in ops:
        if cmd.startswith(op, i):
            if op == "&" and (cmd[i - 1:i] in ("<", ">") or cmd[i + 1:i + 2] == ">"):
                return None  # redirezione (2>&1, &>file), non un separatore
            return op
    return None


def split_compound(cmd: str, ops: tuple[str, ...]) -> list[str]:
    subs, cur, quote, i = [], [], None, 0
    while i < len(cmd):
        ch = cmd[i]
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            cur.append(ch)
            i += 1
            continue
        op = _op_at(cmd, i, ops)
        if op:
            subs.append("".join(cur))
            cur = []
            i += len(op)
            continue
        cur.append(ch)
        i += 1
    subs.append("".join(cur))
    return [s.strip() for s in subs if s.strip()]


def strip_bash(sub: str) -> str:
    s = sub.strip()
    while True:
        m = ASSIGN_RE.match(s)
        if m:
            s = s[m.end():]
            continue
        words = s.split()
        if not words:
            return s
        head, rest = words[0], words[1:]
        if head in WRAPPERS and not (head == "command" and rest[:1] == ["-v"]):
            skip = 1
        elif head == "timeout":
            skip = 1
            while skip < len(words) and words[skip].startswith("-"):
                skip += 1
            skip += 1  # durata
        elif head == "nice":
            skip = 3 if rest[:1] == ["-n"] else 1 + (1 if rest[:1] and rest[0].startswith("-") else 0)
        elif head == "stdbuf":
            skip = 1
            while skip < len(words) and words[skip].startswith("-"):
                skip += 1
        elif head == "xargs" and rest and not rest[0].startswith("-"):
            skip = 1
        else:
            return s
        s = " ".join(words[skip:])


def canon_ps(sub: str) -> str:
    words = sub.split(maxsplit=1)
    if words and words[0].lower() in PS_REMOVE_ALIASES:
        return "Remove-Item" + ("" if len(words) == 1 else " " + words[1])
    return sub


def subcommands(tool: str, command: str) -> list[str]:
    if tool == "Bash":
        return [strip_bash(s) for s in split_compound(command, BASH_OPS)]
    return [canon_ps(s) for s in split_compound(command, PS_OPS)]


def rule_matches(rule: str, tool: str, sub: str) -> bool:
    parsed = parse(rule)
    if parsed is None or parsed[0] != tool:
        return False
    return bool(pattern_regex(parsed[1], tool == "PowerShell").match(sub))


def decide(tool: str, command: str, deny: list[str], ask: list[str]) -> str | None:
    subs = subcommands(tool, command)
    for kind, rules in (("deny", deny), ("ask", ask)):
        if any(rule_matches(r, tool, s) for s in subs for r in rules):
            return kind
    return None


def shared(tool: str, command: str) -> str | None:
    return decide(tool, command, RULES["deny"], RULES["ask"])


def instance(spec: str) -> str:
    out, i = [], 0
    while i < len(spec):
        if spec[i] == "\\" and spec[i + 1:i + 2] in ("\\", "*"):
            out.append(spec[i + 1])
            i += 2
            continue
        # il jolly diventa un flag: un wrapper seguito da flag (xargs -0 ...) non viene tolto
        out.append("-Z" if spec[i] == "*" else spec[i])
        i += 1
    return "".join(out)


def command_rules() -> list[tuple[str, str]]:
    out = [(k, r) for k in ("deny", "ask") for r in RULES[k]]
    out += [("deny", r) for r in DS["permissions"]["deny"] + SC["permissions"]["deny"] if parse(r)]
    return out


# --- file hygiene -------------------------------------------------------------------------------

@pytest.mark.parametrize("path", ALL_FILES, ids=lambda p: p.name)
def test_file_is_clean_json(path: Path) -> None:
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in raw
    text = raw.decode("utf-8")
    assert chr(0x2014) not in text and chr(0x2013) not in text
    data = json.loads(text)
    perms = data if path == RULES_FILE else data["permissions"]
    for kind in ("deny", "ask"):
        items = perms.get(kind, [])
        assert len(items) == len(set(items)), f"duplicate {kind} rule in {path.name}"


def test_shared_rules_are_shell_rules_only() -> None:
    assert set(RULES) == {"_comment", "deny", "ask"}
    for rule in RULES["deny"] + RULES["ask"]:
        assert parse(rule) is not None, rule


# Il repository e pubblico: nessun file deve contenere il nome utente del portatile
SCAN_DIRS = ("scripts", "hooks", "bin", "lib", "docs", "agents")
SCAN_SKIP = {"__pycache__", ".venv"}


def test_no_literal_username_in_repo() -> None:
    root = SETTINGS.parents[1]
    needle = ("berni" + "." + "alberto").encode("utf-8")  # composto: questo file non lo contiene
    files = [root / "README.md"]
    for name in SCAN_DIRS:
        files += [p for p in (root / name).rglob("*")
                  if p.is_file() and not SCAN_SKIP & set(p.relative_to(root).parts)]
    hits = [p.relative_to(root).as_posix() for p in files if needle in p.read_bytes()]
    assert hits == []


@pytest.mark.parametrize("kind,rule", command_rules())
def test_no_lone_backslash(kind: str, rule: str) -> None:
    # B3: una barra rovescia deve sempre formare la coppia doppia-barra o barra-asterisco
    spec = parse(rule)[1]
    i = 0
    while i < len(spec):
        if spec[i] == "\\":
            assert spec[i + 1:i + 2] in ("\\", "*"), f"lone backslash in {rule}"
            i += 2
        else:
            i += 1


@pytest.mark.parametrize("kind,rule", command_rules())
def test_every_rule_fires_on_its_own_instance(kind: str, rule: str) -> None:
    tool, spec = parse(rule)
    assert decide(tool, instance(spec), [rule], []) == "deny", f"{kind} rule never fires: {rule}"


# --- shared rules: realistic corpus -------------------------------------------------------------

DENY_CASES = [
    ("Bash", "rm -rf /"), ("Bash", "rm -rf /*"), ("Bash", "rm -rf /c"), ("Bash", "rm -rf '/c/'"),
    ("Bash", "rm -rf ~"), ("Bash", "rm -rf ~/*"), ("Bash", "rm -rf ~/code"), ("Bash", "rm -rf ~/code/"),
    ("Bash", "rm -rf ~/code/*"), ("Bash", "rm ~/code -rf"), ("Bash", 'rm -rf "$HOME/code"'),
    ("Bash", 'rm -rf "${HOME}"'), ("Bash", "rm -rf $HOME/*"), ("Bash", "rm -rf /c/Users/someone/code"),
    ("Bash", "rm -rf C:/Users/someone/code/*"), ("Bash", "rm -rf ~/.claude"),
    ("Bash", 'rm -rf "/c/Users/x/.claude-deepseek/"'), ("Bash", "rm -rf ~/.claude/agent-memory"),
    ("Bash", "rm -fr ~/.claude-deepseek/agent-memory/*"), ("Bash", "rm -rf ~/.claude/agent-memory -v"),
    ("Bash", "cd /tmp && rm -rf ~/code"), ("Bash", "timeout 30 rm -rf ~/code"), ("Bash", "FOO=1 rm -rf ~"),
    ("Bash", "echo start; rm -rf ~/code/* | tee log"), ("Bash", "diskpart /s script.txt"),
    ("PowerShell", r"Remove-Item C:\Users\someone\code -Recurse -Force"),
    ("PowerShell", r"Remove-Item -Recurse -Force C:\Users\someone\code"),
    ("PowerShell", r"Remove-Item 'C:\Users\someone\code' -Recurse"),
    ("PowerShell", r'Remove-Item "$env:USERPROFILE\code\*" -Recurse'),
    ("PowerShell", r"Remove-Item ~\code\* -Recurse -Force"), ("PowerShell", "rm -r -fo $HOME"),
    ("PowerShell", "Remove-Item -Recurse -Force ~"), ("PowerShell", "Remove-Item C:\\ -Recurse"),
    ("PowerShell", "remove-item c:/users/x/code -recurse"),
    ("PowerShell", "Remove-Item -Recurse ~/.claude/agent-memory"),
    ("PowerShell", r'Remove-Item -Recurse "$HOME\.claude-deepseek"'),
    ("PowerShell", "Get-ChildItem x; Remove-Item -Recurse ~/code"),
    ("PowerShell", "Format-Volume -DriveLetter D"),
    # M1: home e disco C: per percorso assoluto o $USERPROFILE
    ("Bash", "rm -rf /c/Users/x"), ("Bash", "rm -rf C:/Users/x/"),
    ("Bash", 'rm -rf "$USERPROFILE"'), ("Bash", "rm -rf ${USERPROFILE}/code"), ("Bash", "rm -rf C:/"),
    ("Bash", "rm -rf C:/*"), ("Bash", "rm -rf /c/Users"), ("Bash", r"rm -rf 'C:\Users\x'"),
    ("PowerShell", r"Remove-Item -Recurse -Force C:\Users\x"),
    ("PowerShell", "Remove-Item -Recurse C:/Users/x/"), ("PowerShell", r"Remove-Item C:\Users -Recurse"),
    ("Bash", r"cmd /c rd /s /q C:\ "), ("Bash", "cmd /c rd /s /q C:\\"), ("PowerShell", "cmd /c rd /s /q C:\\"),
    ("PowerShell", r"cmd /c rd /s /q C:\Users\x"), ("Bash", r'cmd //c "rd //s //q C:\Users\"'),
    ("PowerShell", r"cmd /c del /s /q C:\*"),
    # m3: strumenti di disco con suffisso .exe
    ("Bash", "diskpart.exe /s x"), ("PowerShell", "diskpart.exe /s x"), ("Bash", "format.exe D: /q"),
]

ASK_CASES = [
    ("Bash", "rm -r build/"), ("Bash", "rm -f -r build/"), ("Bash", "rm --force --recursive build/"),
    ("Bash", "rm build -rf"), ("Bash", "rm -rf ~/code/proj/build"), ("Bash", "rm -rf ~/codebase"),
    ("Bash", "git reset --hard HEAD~1"), ("Bash", "git -C ~/code/x reset --hard"),
    ("Bash", "git -c core.x=y reset --hard origin/main"), ("Bash", "git -C ~/code/x push --force origin main"),
    ("Bash", "git -C ~/code/x push -f"), ("Bash", "git -C /c/repo clean -fdx"),
    ("Bash", "git -C ~/code/x branch -D old"), ("Bash", "git -C ~/code/x checkout -- ."),
    ("Bash", "git -C ~/code/x restore ."), ("Bash", "git -C ~/code/x stash drop"),
    ("Bash", "git -C ~/code/x stash clear"), ("Bash", "git -C ~/code/x filter-branch --tree-filter t HEAD"),
    ("Bash", "nohup git -C ~/code/x reset --hard"), ("Bash", "cmd /c rd /s /q build"),
    ("Bash", 'cmd //c "rd //s //q build"'), ("Bash", "cmd /c del /s /q old.tmp"),
    ("Bash", "cmd.exe /c rmdir /s build"), ("Bash", "cmd /c erase /s old.tmp"),
    ("Bash", 'sqlite3 data.db "DROP TABLE users;"'), ("Bash", 'psql -c "drop table x"'),
    ("Bash", 'duckdb db.duckdb -c "TRUNCATE t"'), ("Bash", 'mysql -e "DROP DATABASE prod"'),
    ("Bash", "python -c \"con.execute('DROP SCHEMA s')\""),
    ("Bash", ".venv/Scripts/python.exe -c \"c.execute('truncate table t')\""),
    ("PowerShell", "Remove-Item build -Recurse"), ("PowerShell", "Remove-Item -Recurse build"),
    ("PowerShell", "rm -r build"), ("PowerShell", r"Remove-Item C:\Users\x\code\proj\build -Recurse"),
    ("PowerShell", r"Remove-Item ~\.vscode -Recurse"), ("PowerShell", r"git -C C:\repo reset --hard"),
    ("PowerShell", "git -C ~/code/x push --force"), ("PowerShell", "cmd /c rd /s /q build"),
    ("PowerShell", 'sqlite3 db "DROP TABLE t"'), ("PowerShell", "python -c \"x.execute('truncate t')\""),
    # documentato: PowerShell non distingue -D da -d
    ("PowerShell", "git branch -d feature"),
    # M2: cancellazioni ricorsive che non iniziano con rm
    ("Bash", "find ~/code -mindepth 1 -delete"), ("Bash", "find ~/code -name build -exec rm -rf {} +"),
    ("Bash", "find . -type d -execdir rm -r {} ;"), ("Bash", "ls ~/code | xargs -I{} rm -rf ~/code/{}"),
    ("Bash", "find . -name '*.pyc' -print0 | xargs -0 rm -f"),
    ("PowerShell", "find . -name '*.tmp' -delete"), ("PowerShell", "ls | xargs -I{} rm -rf {}"),
    # m3: git distruttivo in forme alternative
    ("Bash", "git checkout HEAD -- ."), ("Bash", "git -C ~/code/x checkout HEAD~1 -- ."),
    ("Bash", "git branch --delete --force old"), ("Bash", "git push origin +main"),
    ("PowerShell", "git -C ~/code/x push origin +main"),
    # nessuna sovra-cancellazione: sottocartelle della home restano ask
    ("Bash", "rm -rf /c/Users/x/code/proj/build"),
    ("PowerShell", r"Remove-Item C:\Users\x\Downloads\old -Recurse"),
    # m2, sovra-corrispondenza documentata: il matcher non salta il testo tra virgolette
    ("Bash", 'git -C ~/code/x commit -m "docs: explain reset --hard"'),
    ("PowerShell", "python summarize.py --truncate 80"),
]

NONE_CASES = [
    ("Bash", "git push origin feature-fix"), ("Bash", "git push origin main"),
    ("Bash", "git -C ~/code/x status"), ("Bash", 'git -C ~/code/x commit -m "fix typo"'),
    ("Bash", "git -C ~/code/x log --oneline -5"), ("Bash", "git branch -d feature"),
    ("Bash", "git checkout main"), ("Bash", "git clean -n"), ("Bash", "rm -f report.txt"),
    ("Bash", "rm report-results.csv"), ("Bash", "rm backup~"), ("Bash", "rm -f ~/code/x/notes.txt"),
    ("Bash", "cmd /c dir /s /b"), ("Bash", 'python -c "f.truncate()"'), ("Bash", "echo DROP TABLE x"),
    ("Bash", "ls -la ~/code"), ("Bash", "cat x 2>&1 | head"),
    ("PowerShell", "Remove-Item report-results.csv"), ("PowerShell", r"Remove-Item .\build\out.txt"),
    ("PowerShell", "Remove-Item -Force notes.txt"), ("PowerShell", "git -C ~/code/x status"),
    ("PowerShell", "git push origin feature-fix"), ("PowerShell", r"Get-ChildItem ~\code"),
    ("PowerShell", "cmd /c dir /s /b"), ("PowerShell", r"Remove-ItemProperty -Path HKCU:\x -Name y"),
    ("Bash", "find ~/code -name '*.py' -newer x"), ("Bash", "ls | xargs -n1 echo"),
    ("Bash", "git push origin main --tags"), ("Bash", "cmd /c dir C:\\"),
]


@pytest.mark.parametrize("tool,command", DENY_CASES)
def test_shared_deny(tool: str, command: str) -> None:
    assert shared(tool, command) == "deny"


@pytest.mark.parametrize("tool,command", ASK_CASES)
def test_shared_ask(tool: str, command: str) -> None:
    assert shared(tool, command) == "ask"


@pytest.mark.parametrize("tool,command", NONE_CASES)
def test_shared_no_match(tool: str, command: str) -> None:
    assert shared(tool, command) is None


# --- deepseek template --------------------------------------------------------------------------

DS_DENY = DS["permissions"]["deny"]
KEY_COMMANDS = [
    ("Bash", "C:/Users/x/.claude-deepseek/.venv/Scripts/python.exe C:/Users/x/.claude-deepseek/bin/key-helper.py"),
    ("Bash", "python ~/.claude-deepseek/bin/store-key.py"), ("Bash", "python -m keyring get svc user"),
    ("Bash", "keyring get svc user"), ("Bash", "cmdkey /list"),
    ("PowerShell", "& 'C:/Users/x/.claude-deepseek/.venv/Scripts/python.exe' 'C:/Users/x/.claude-deepseek/bin/key-helper.py'"),
    ("PowerShell", "python -c \"import keyring; print(keyring.get_password('a', 'b'))\""),
    ("PowerShell", "cmdkey /list"),
]


def test_ds_denies_monitor_and_claude_json() -> None:
    for rule in ("Monitor", "Read(~/.claude.json)", "Edit(~/.claude.json)",
                 "Read(~/.claude.json.backup*)", "Edit(~/.claude.json.backup*)"):
        assert rule in DS_DENY


@pytest.mark.parametrize("tool,command", KEY_COMMANDS)
def test_ds_denies_key_commands(tool: str, command: str) -> None:
    assert decide(tool, command, DS_DENY + RULES["deny"], RULES["ask"]) == "deny"


def test_ds_model_aliases_pinned() -> None:
    env = DS["env"]
    for var in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL"):
        assert env[var] == "deepseek-v4-pro", var
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"].startswith("deepseek-")
    model_vars = [k for k in env if k.startswith("ANTHROPIC_") and k.endswith("MODEL")]
    assert all(env[k].startswith("deepseek-") for k in model_vars)
    assert "[1m]" not in DS_FILE.read_text(encoding="utf-8")


# --- superclaude template -----------------------------------------------------------------------

SC_DENY = SC["permissions"]["deny"]
DS_DIRS = ("agent-memory", "plans", "comms", "projects", "state", "tmp", "logs")


def test_sc_template_shape() -> None:
    # contratto R-1 con l'applicatore: stessa forma di deepseek.base.json, solo permissions.deny/ask
    assert set(SC) <= {"_comment", "permissions"}
    assert set(SC["permissions"]) <= {"deny", "ask"}
    for rule in SC_DENY:
        if rule.startswith(("Read(", "Edit(")):
            assert rule[5:].startswith("~/.claude-deepseek/"), rule


def test_sc_denies_ds_data() -> None:
    for d in DS_DIRS:
        for tool in ("Read", "Edit"):
            assert f"{tool}(~/.claude-deepseek/{d}/**)" in SC_DENY
    for f in (".credentials.json", ".claude.json"):
        for tool in ("Read", "Edit"):
            assert f"{tool}(~/.claude-deepseek/{f})" in SC_DENY
    assert "Edit(~/.claude-deepseek/settings*.json)" in SC_DENY


@pytest.mark.parametrize("tool,command", KEY_COMMANDS)
def test_sc_denies_key_commands(tool: str, command: str) -> None:
    assert decide(tool, command, SC_DENY + RULES["deny"], RULES["ask"]) == "deny"


@pytest.mark.parametrize("tool,command", [
    ("Bash", "git -C ~/code/x status"), ("Bash", "python -m pytest tests/test_key_helper.py -q"),
    ("Bash", "ls ~/.claude-deepseek/bin"), ("PowerShell", "Get-Content notes.md"),
    ("PowerShell", "python -m pip list"),
])
def test_sc_key_rules_leave_normal_commands(tool: str, command: str) -> None:
    assert decide(tool, command, SC_DENY + RULES["deny"], RULES["ask"]) is None
