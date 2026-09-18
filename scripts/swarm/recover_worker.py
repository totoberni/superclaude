#!/usr/bin/env python3
"""recover_worker.py - forensic extraction from Claude Code worker artifacts,
for /recover-truncated (or manual invocation), when a background task or
subagent needs a compact summary pulled out of its raw output without
dumping raw JSONL into an LLM context window.

Usage:
  recover_worker.py task <output-file>
    <output-file> is a background task output file, pattern:
    .../tasks/<task-id>.output

  recover_worker.py agent <jsonl-file>
    <jsonl-file> is a subagent transcript, pattern:
    ~/.claude/projects/<project-slug>/<session-id>/subagents/agent-<agentId>.jsonl

Both modes hard-cap stdout to roughly 3000 characters total and never
print an entire raw JSONL file.

Port Python (solo stdlib, niente jq) di recover-worker.sh: stessa CLI,
stesso formato di output, stessi codici di uscita.
"""
import json
import os
import sys

STDOUT_CAP = 3000
AGENT_TEXT_CAP = 2000


def err(msg):
    print("ERROR: %s" % msg, file=sys.stderr)


def usage():
    print("usage: recover_worker.py task <output-file>", file=sys.stderr)
    print("       recover_worker.py agent <jsonl-file>", file=sys.stderr)


def cap(text, max_chars):
    """Tronca text a max_chars e annota il troncamento (come cap() nel bash)."""
    if len(text) > max_chars:
        return "%s\n[... truncated at %d of %d chars ...]" % (
            text[:max_chars], max_chars, len(text))
    return text


def _read_lines(path):
    """Righe del file, tolleranti a byte non UTF-8 (transcript troncati)."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read().splitlines()


def _last_nonempty(lines):
    for line in reversed(lines):
        if line.strip():
            return line
    return ""


def _parse(line):
    """json.loads che non solleva mai: None se la riga non e' un oggetto JSON."""
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _content_of(obj):
    msg = obj.get("message") if isinstance(obj, dict) else None
    return msg.get("content") if isinstance(msg, dict) else None


def mode_task(path):
    """Intestazione + testo del risultato finale. Ritorna (rc, output)."""
    if not os.path.isfile(path):
        err("task output file not found: %s" % path)
        return 1, ""
    lines = _read_lines(path)

    result = ""
    obj = _parse(lines[-1]) if lines else None
    content = _content_of(obj)
    if isinstance(content, list) and content and isinstance(content[0], dict):
        text = content[0].get("text")
        if isinstance(text, str):
            result = text
    elif isinstance(content, str):
        # .message.content puo' essere una stringa nuda, non un array.
        result = content

    if not result:
        # Fallback: ultima riga grezza non vuota.
        result = _last_nonempty(lines)

    if not result:
        err("could not extract a result from %s" % path)
        return 1, ""
    return 0, "## Task output recovery: %s\n\n%s" % (path, result)


def mode_agent(path):
    """Ultimo testo dell'assistant, conteggio tool_use e percorsi Write/Edit
    deduplicati. Ritorna (rc, output)."""
    if not os.path.isfile(path):
        err("agent transcript file not found: %s" % path)
        return 1, ""
    lines = _read_lines(path)

    last_text = ""
    tool_use_count = 0
    file_paths = set()
    parsed_any = False
    for line in lines:
        # Una riga finale troncata o corrotta viene semplicemente scartata.
        obj = _parse(line)
        if obj is None:
            continue
        parsed_any = True
        if obj.get("type") != "assistant":
            continue
        content = _content_of(obj)
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    last_text = text
            elif block.get("type") == "tool_use":
                tool_use_count += 1
                if block.get("name") in ("Write", "Edit"):
                    inp = block.get("input")
                    fp = inp.get("file_path") if isinstance(inp, dict) else None
                    if isinstance(fp, str) and fp:
                        file_paths.add(fp)

    if not last_text:
        # Fallback: forma inattesa del transcript, nessun blocco di testo.
        last_text = _last_nonempty(lines) or "(no assistant text message found)"

    out = [
        "## Agent transcript recovery: %s" % path,
        "",
        "### Last assistant text message",
        cap(last_text, AGENT_TEXT_CAP),
        "",
        "### tool_use event count",
        str(tool_use_count) if parsed_any else "unknown",
        "",
        "### Write/Edit file paths touched (deduplicated)",
    ]
    out.extend(sorted(file_paths) if file_paths else ["(none)"])
    return 0, "\n".join(out)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    # Console Windows cp1252: il testo recuperato contiene spesso caratteri non ASCII.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    mode = argv[0] if argv else ""
    target = argv[1] if len(argv) > 1 else ""
    if mode not in ("task", "agent") or not target:
        usage()
        return 1
    target = os.path.expanduser(target)
    rc, output = mode_task(target) if mode == "task" else mode_agent(target)
    if rc != 0:
        return rc
    print(cap(output, STDOUT_CAP))
    return 0


if __name__ == "__main__":
    sys.exit(main())
