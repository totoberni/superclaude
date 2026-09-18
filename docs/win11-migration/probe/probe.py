"""Hook sonda (Fase 1): registra cosa Claude Code passa davvero agli hook su Windows.

Solo libreria standard, nessuna dipendenza. Non blocca mai nulla: esce sempre con 0.
Output: un file JSON per invocazione in ~/.claude/tmp/probe/.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

T0 = time.perf_counter()
OUT_DIR = Path.home() / ".claude" / "tmp" / "probe"
TOKEN = "PROBE-TOKEN-7431"

# Variabili d'ambiente di cui ci interessa anche il valore (nessun segreto)
ENV_VALUES = (
    "CLAUDE_PROJECT_DIR", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_AGENT_NAME", "CLAUDE_ENV_FILE", "CLAUDE_CODE_GIT_BASH_PATH",
    "CLAUDE_COWORK_MEMORY_GUIDELINES", "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
    "SHELL", "MSYSTEM", "HOME", "USERPROFILE", "TMPDIR", "TEMP",
    "HF_HUB_OFFLINE", "PYTHONUTF8",
)


def main() -> int:
    raw = sys.stdin.buffer.read()
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:  # noqa: BLE001 - la sonda non deve mai fallire
        payload = {"_parse_error": repr(exc)}

    event = payload.get("hook_event_name", "unknown") if isinstance(payload, dict) else "unknown"

    # Il contenuto degli strumenti può essere lungo o sensibile: teniamo solo la forma
    slim = payload
    if isinstance(payload, dict):
        slim = dict(payload)
        for key in ("tool_response", "last_assistant_message", "prompt"):
            if key in slim:
                slim[key] = f"<omesso: {type(slim[key]).__name__}>"
        ti = slim.get("tool_input")
        if isinstance(ti, dict):
            slim["tool_input"] = {
                k: (v if k in ("file_path", "path", "notebook_path", "subagent_type", "description")
                    else f"<omesso: {type(v).__name__}>")
                for k, v in ti.items()
            }

    record = {
        "event": event,
        "stdin_first_bytes_hex": raw[:3].hex(),
        "stdin_has_crlf": b"\r\n" in raw,
        "payload": slim,
        "argv": sys.argv,
        "cwd": os.getcwd(),
        "python": sys.executable,
        "env_values": {k: os.environ.get(k) for k in ENV_VALUES},
        "env_claude_names": sorted(k for k in os.environ if k.upper().startswith("CLAUDE")),
        "path_head": os.environ.get("PATH", "").split(os.pathsep)[:6],
        "elapsed_ms_before_write": round((time.perf_counter() - T0) * 1000, 1),
    }

    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        name = f"{int(time.time() * 1000)}-{event}-{os.getpid()}.json"
        (OUT_DIR / name).write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    # Verifica se additionalContext arriva al modello dai vari eventi
    if event in ("SessionStart", "PostToolUse", "PreToolUse", "SubagentStart"):
        out = {
            "hookSpecificOutput": {
                "hookEventName": event,
                "additionalContext": f"{TOKEN} event={event}",
            }
        }
        sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
