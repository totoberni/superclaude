"""Test della guardia wait: riscritture meccaniche, blocchi precisi, nessun falso positivo.

I positivi includono comandi reali presi dalle trascrizioni degli agenti (attese che
hanno tenuto fermi dei worker per minuti; le righe troncate sono state completate in
modo plausibile). I negativi coprono testo tra virgolette, heredoc e commenti, che non
devono mai far scattare la guardia, e le forme legittime (ritmi brevi, watchdog in
background, find circoscritti, job.py).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time

import pytest

from sc_hooks import core
from sc_hooks.handlers import wait_guard

SCR = "C:/Users/berni.alberto/AppData/Local/Temp/claude/C--Users-berni-alberto/0f9d1124/scratchpad"


def run(mk, cmd, tool="Bash", role="plain", agent=None, **ti):
    return wait_guard.handle(mk(tool, role=role, agent=agent, command=cmd, **ti))


def blocked(res) -> str:
    assert res is not None and res.block, f"atteso un blocco, ottenuto {res!r}"
    assert res.updated_input is None
    return res.block


def rewritten(res) -> str:
    assert res is not None and not res.block and res.updated_input is not None, f"attesa una riscrittura: {res!r}"
    return res.updated_input["command"]


# --------------------------------------------------------------------------
# Cicli di attesa (Bash): until/while con sleep, for con sleep e uscita
# --------------------------------------------------------------------------
POLL_LOOPS = [
    "until grep -q DONE /tmp/x.log; do sleep 10; done; tail -n 6 /tmp/x.log",
    "while ! grep -q 'network done' \"$L\"; do sleep 5; done",
    "while ! grep -q x f\ndo\n  sleep 5\ndone",
    "while sleep 5; do test -f x && break; done",
    "( while true; do sleep 1; done )",
    "until curl -sf http://localhost:8000; do sleep 2; done",
    # forme reali
    f'cd C:/Users/berni.alberto; until [ -f "{SCR}/e/done.json" ]; do sleep 10; done; cat "{SCR}/e/done.json"',
    "until [ -s /c/Users/berni.alberto/.claude/tmp/memsync-t12/mutate.out ]; do sleep 5; done; "
    "cat /c/Users/berni.alberto/.claude/tmp/memsync-t12/mutate.out",
    "until [ -s /c/x/mutate.out ] && ! pgrep -f mutate.py >/dev/null 2>&1; do sleep 5; done; cat /c/x/mutate.out",
    "until grep -q \"^done\" C:/Users/berni.alberto/.claude/tmp/memsync-t14a-fix/full.txt; do sleep 10; done; "
    "tail -n 6 C:/Users/berni.alberto/.claude/tmp/memsync-t14a-fix/full.txt",
    f'until grep -q "network done" {SCR}/s6/probe_istat/run_today.out; do sleep 30; done; echo ok',
    f'L="{SCR}/V2RNA/download.log"; until grep -qE "download completato|STOP|Traceback" "$L"; do sleep 20; done; '
    'tail -n 5 "$L"',
    f"L={SCR}/perf/perf.log; until grep -qE \"^exit \" \"$L\"; do sleep 3; done; tr '\\r' '\\n' < \"$L\" | tail -n 5",
    f'S="{SCR}"; until grep -q "STOP\\|fine rete" "$S/s10/probe/run.out"; do sleep 20; done; '
    'tail -n 20 "$S/s10/probe/run.out"',
    f"until grep -q EXIT {SCR}/istat_run.out; do sleep 10; done; tail -n +10 {SCR}/istat_run.out",
    f'L={SCR}/perf/perf.log; while true; do if grep -qE "^exit " "$L"; then grep -E "^exit " "$L"; break; fi; '
    "sleep 3; done",
    f'until grep -qE "Transaction finished|micromamba activate|error|Traceback|CondaError|failed" "{SCR}/i.log"; '
    "do sleep 15; done",
]


@pytest.mark.parametrize("cmd", POLL_LOOPS)
def test_poll_loops_are_blocked(mk, cmd):
    msg = blocked(run(mk, cmd))
    assert "loop with sleep polls" in msg
    assert "scpy job.py wait <id> --max 540" in msg and "scpy job.py start --name <id> -- <cmd>" in msg
    assert "run_in_background" in msg
    assert "timeout 25 bash -c '...'" in msg          # via di fuga per un ciclo breve e limitato


def test_loop_escape_only_on_loop_blocks(mk):
    assert "timeout 25 bash -c" not in blocked(run(mk, "sleep 60"))


@pytest.mark.parametrize("cmd", [
    "for i in $(seq 1 60); do [ -f /c/Users/berni.alberto/.claude/plans/cv-refresh/checkpoints/T11a-shine.md ] "
    "&& { echo FOUND after $i; exit 0; }; sleep 15; done; echo NOTFOUND; exit 1",
    "for i in 1 2 3 4 5; do test -f x && break; sleep 2; done",
    "for ((i=0; i<60; i++)); do sleep 1; [ -f x ] && break; done",
    "for f in a b; do while read l; do sleep 1; done < $f; [ -s out ] && break; done",
])
def test_for_poll_until_found_is_blocked(mk, cmd):
    assert "sleeps until a check breaks out" in blocked(run(mk, cmd))


@pytest.mark.parametrize("cmd", [
    "for u in a b; do curl -s $u; sleep 15; done",
    "for i in 1 2 3; do echo $i; sleep 1; done",
    "while read u; do curl -s \"$u\"; sleep 15; done < urls.txt",
    "while IFS= read -r line; do echo \"$line\"; sleep 0.2; done < f.txt",
    "for f in *.py; do python \"$f\"; done",
    "while read l; do echo $l; done < f",
    "until [ -f x ]; do :; done",
])
def test_pacing_and_sleepless_loops_pass(mk, cmd):
    assert run(mk, cmd) is None


# --------------------------------------------------------------------------
# sleep, Start-Sleep, timeout /t
# --------------------------------------------------------------------------
@pytest.mark.parametrize("cmd,secs", [
    ("sleep 30", "30 s"), ("sleep 60", "60 s"), ("sleep 1m", "60 s"), ("sleep 0.5m", "30 s"),
    ("sleep 1h", "3600 s"), ("sleep 20 20", "40 s"), ("sleep infinity", "forever"),
    ("sleep 45; cat out", "45 s"), ("cd /c/x && sleep 120 && ls", "120 s"), ("x=$(sleep 60; echo hi)", "60 s"),
    ("timeout /t 30", "30 s"), ("timeout 900 sleep 300", "300 s"),
])
def test_long_sleep_bash_blocked(mk, cmd, secs):
    msg = blocked(run(mk, cmd))
    assert f"waits {secs}" in msg and "30 s or more are blocked" in msg


@pytest.mark.parametrize("cmd,secs", [
    ("Start-Sleep 30", "30 s"), ("Start-Sleep -Seconds 60", "60 s"), ("Start-Sleep -s 45", "45 s"),
    ("Start-Sleep -Seconds:90", "90 s"), ("Start-Sleep -Milliseconds 30000", "30 s"),
    ("Start-Sleep -m 60000", "60 s"), ("start-sleep -ms 45000", "45 s"), ("sleep 40", "40 s"),
    ("timeout /t 60", "60 s"), ("timeout /t 60 /nobreak", "60 s"), ("timeout /t -1", "forever"),
    ("timeout 45", "45 s"), ("Get-Date; Start-Sleep -Seconds 120; Get-Date", "120 s"),
])
def test_long_sleep_powershell_blocked(mk, cmd, secs):
    assert f"waits {secs}" in blocked(run(mk, cmd, "PowerShell"))


@pytest.mark.parametrize("tool,cmd", [
    ("Bash", "sleep 2; cat out"), ("Bash", "sleep 29"), ("Bash", "sleep 29.9"), ("Bash", "sleep $N"),
    ("Bash", "sleep 10 && ls"), ("Bash", "echo sleep 60"), ("Bash", 'pkill -f "sleep 60"'),
    ("Bash", "ps aux | grep sleep"), ("Bash", "timeout 600 pytest -q"),
    ("Bash", f'sleep 2; cat "{SCR}\\tasks\\bym9ibrq6.output"'),
    ("PowerShell", "Start-Sleep 2"), ("PowerShell", "Start-Sleep -Milliseconds 500"),
    ("PowerShell", "Start-Sleep -Duration (New-TimeSpan -Minutes 5)"), ("PowerShell", "timeout 600 pytest -q"),
])
def test_short_or_unknown_sleeps_pass(mk, tool, cmd):
    assert run(mk, cmd, tool) is None


# --------------------------------------------------------------------------
# Cicli di PowerShell
# --------------------------------------------------------------------------
@pytest.mark.parametrize("cmd", [
    "while (-not (Test-Path x)) { Start-Sleep 5 }",
    "while($true){if(Test-Path x){break}; Start-Sleep -Seconds 2}",
    "do { Start-Sleep 2 } until (Test-Path x)",
    "do{Start-Sleep 1}while(-not $done)",
    "foreach ($i in 1..60) { if (Test-Path x) { break }; Start-Sleep 5 }",
    "for ($i = 0; $i -lt 60; $i++) { if (Test-Path x) { break }; Start-Sleep 5 }",
    "1..60 | ForEach-Object { if (Test-Path x) { exit 0 }; Start-Sleep 5 }",
    "While (-not (Get-Job -State Completed)) {\n    Start-Sleep -Seconds 3\n}",
])
def test_powershell_poll_loops_blocked(mk, cmd):
    msg = blocked(run(mk, cmd, "PowerShell"))
    assert "polls and keeps you waiting" in msg and "timeout 25 bash -c" in msg


@pytest.mark.parametrize("cmd", [
    # `.Timeout`, `.Sleep` e le parole qualsiasi non sono un'attesa (solo la posizione di comando conta)
    "while ($i -lt 3) { $c.Timeout = 5; $i++ }",
    "while ($i -lt 3) { Write-Host connection timeout; $i++ }",
    "while ($i -lt 3) { Write-Host sleep; $i++ }",
    "foreach ($s in $servers) { if ($s.Timeout -gt 5) { break } }",
    "$files | ForEach-Object { if ($_.Timeout -gt 3) { break } }",
    "foreach ($f in $files) { $r = $f.Sleep; if ($r) { break } }",
    "foreach ($u in $urls) { Invoke-WebRequest $u; Start-Sleep 15 }",
    "while ($i -lt 3) { $i++ }",
    "# while ($true) { Start-Sleep 5 }",
    "<# while ($true) { Start-Sleep 5 } #>\nGet-Date",
    "$s = @'\nwhile ($true) { Start-Sleep 60 }\n'@\nWrite-Output $s",
    'Write-Host "while ($true) { Start-Sleep 60 }"',
    "Get-ChildItem | ForEach-Object { $_.Name }",
])
def test_powershell_non_polls_pass(mk, cmd):
    assert run(mk, cmd, "PowerShell") is None


# --------------------------------------------------------------------------
# find sulla radice o sulla home
# --------------------------------------------------------------------------
@pytest.mark.parametrize("cmd", [
    "find / -name x", "find /c -name x", "find /c/ -name x", "find C:/ -name x",
    "find 'C:\\' -name x", r'find "C:\\" -name x', "find ~ -name x", "find ~/ -name x",
    "find $HOME -name x", 'find "${HOME}" -name x', "find /c/Users/berni.alberto -name x",
    "find /c/Users/berni.alberto/ -name x", "find C:/Users/berni.alberto -name x",
    r"find 'C:\Users\berni.alberto' -name x", "find /c/Users -name x", "find -L / -name x",
    "find /c/x /c -name y", "find / -maxdepth 3 -name x",
    # forme reali
    'cd /c/Users/berni.alberto/code/personal/cv/v1; ls build | head -30; '
    'find / -ipath "*rendercv*" -name "*.typ" 2>/dev/null | head',
    'cd /c/Users/berni.alberto/code/personal/cv/v1; find / -maxdepth 8 -type d -ipath "*site-packages/fontTools" '
    "2>/dev/null | head; ls .venv/Lib/site-packages | head",
])
def test_wide_find_blocked(mk, cmd):
    msg = blocked(run(mk, cmd))
    assert "walks a whole drive or profile" in msg and "scope the path or use the Glob tool" in msg


@pytest.mark.parametrize("tool,cmd", [
    ("Bash", "find . -name x"), ("Bash", "find /c/Users/berni.alberto/code -maxdepth 2 -name y"),
    ("Bash", "find ~/code -name x"), ("Bash", 'find $HOME/.claude/hooks -name "*.py"'),
    ("Bash", "find / -maxdepth 1 -name x"), ("Bash", 'find ~ -maxdepth 2 -name "*.md"'),
    ("Bash", "find ~ -maxdepth 4 -name x"), ("Bash", 'find "${HOME}" -maxdepth 4 -name x'),
    ("Bash", "find C:/Users/berni.alberto -maxdepth 4 -name x"),
    # forme reali: home a profondita' 3 (0.4 s misurati)
    ("Bash", 'cd /c/Users/berni.alberto; ls ~/.claude/plans/databricks-tracker-inv; find ~ -maxdepth 3 '
             '-iname "*supermem*" -not -path "*/AppData/*" 2>/dev/null | head'),
    ("Bash", "ls -A /c/Users/berni.alberto/code; find /c/Users/berni.alberto -maxdepth 3 -type d "
             "\\( -iname ExcITA -o -iname GeoScraper \\) 2>/dev/null | head"),
    ("Bash", 'grep -rn "find /" .'), ("Bash", "echo find /"), ("PowerShell", 'find "x" file.txt'),
])
def test_scoped_find_passes(mk, tool, cmd):
    assert run(mk, cmd, tool) is None


@pytest.mark.parametrize("cmd", [
    "find ~ -maxdepth 5 -name x", "find /c/Users/berni.alberto -maxdepth 6 -name x",
    "find /c -maxdepth 3 -name x", "find /c/Users -maxdepth 3 -name x", "find ~ / -maxdepth 3 -name x",
])
def test_find_depth_limit_is_per_root(mk, cmd):
    assert "walks a whole drive or profile" in blocked(run(mk, cmd))


# --------------------------------------------------------------------------
# watch, Wait-Process
# --------------------------------------------------------------------------
@pytest.mark.parametrize("tool,cmd", [
    ("Bash", "watch -n 5 ls"), ("Bash", "watch ls -l"),
    ("PowerShell", "Wait-Process -Id 123"), ("PowerShell", "Get-Process notepad | Wait-Process"),
])
def test_watch_and_wait_process_blocked(mk, tool, cmd):
    blocked(run(mk, cmd, tool))


@pytest.mark.parametrize("tool,cmd", [
    ("Bash", "npm run watch"), ("Bash", "echo watch"),
    ("PowerShell", "Wait-Process -Id 1 -Timeout 30"), ("PowerShell", "Wait-Process -Name x -t 10"),
    ("PowerShell", "Start-Process notepad -Wait"),
])
def test_bounded_waits_pass(mk, tool, cmd):
    assert run(mk, cmd, tool) is None


# --------------------------------------------------------------------------
# Shell annidate
# --------------------------------------------------------------------------
@pytest.mark.parametrize("tool,cmd,needle", [
    ("Bash", f'L="{SCR}/validate/istat/requests.log"; timeout 580 bash -c "while ! grep -q \'network done\' $L; '
             'do sleep 5; done"; tail -n 5 "$L"', "inside `bash`"),
    ("Bash", f'L="{SCR}/validate/istat/requests.log"; date; timeout 580 bash -c "while ! grep -q \'network d\' $L; '
             'do sleep 5; done"', "inside `bash`"),
    ("Bash", "bash -c 'sleep 60'", "waits 60 s"),
    ("Bash", 'sh -c "until [ -f x ]; do sleep 1; done"', "inside `sh`"),
    ("Bash", 'eval "sleep 99"', "inside `eval`"),
    ("Bash", 'powershell -NoProfile -Command "Start-Sleep 60"', "inside `powershell`"),
    ("Bash", "pwsh -c 'while ($true) { Start-Sleep 5 }'", "inside `pwsh`"),
    ("Bash", 'cmd /c "timeout /t 60"', "inside `cmd`"),
    ("Bash", 'bash -lc "tail -f x.log"', "`tail -f` never returns"),
    ("Bash", 'bash -c "python -"', "bare `python -`"),
    ("PowerShell", "bash -c 'sleep 60'", "inside `bash`"),
])
def test_nested_shells_blocked(mk, tool, cmd, needle):
    assert needle in blocked(run(mk, cmd, tool))


@pytest.mark.parametrize("cmd", [
    "bash -c 'echo done'", 'bash script.sh "sleep 60"', 'sh -c "ls; sleep 1"',
    "timeout 20 bash -c 'while true; do date; sleep 1; done'",
    'nohup bash -c "until [ -f x ]; do sleep 5; done" > /dev/null 2>&1 &',
])
def test_harmless_nested_shells_pass(mk, cmd):
    assert run(mk, cmd) is None


# --------------------------------------------------------------------------
# Riscritture
# --------------------------------------------------------------------------
@pytest.mark.parametrize("tool,cmd,expected", [
    ("Bash", "tail -f log.txt", "tail -n 200 log.txt"),
    ("Bash", "tail -F /c/x/run.out", "tail -n 200 /c/x/run.out"),
    ("Bash", "tail --follow=name x.log", "tail -n 200 x.log"),
    ("Bash", "tail --follow x.log", "tail -n 200 x.log"),
    ("Bash", "tail -qF a b", "tail -n 200 -q a b"),
    ("Bash", "cd /c/x && tail -f out.log", "cd /c/x && tail -n 200 out.log"),
    ("Bash", "tail -f x | grep --line-buffered STOP", "tail -n 200 x | grep --line-buffered STOP"),
    ("Bash", "tail -f x | sed 's/quit/x/'", "tail -n 200 x | sed 's/quit/x/'"),
    ("PowerShell", "tail -f x.log", "tail -n 200 x.log"),
    # con un conteggio gia' presente si toglie solo la f: il conteggio dell'autore resta
    ("Bash", "tail -n 20 -f log.txt", "tail -n 20 log.txt"),
    ("Bash", "tail -n +250 -f f", "tail -n +250 f"),
    ("Bash", "tail --lines=5 -f x", "tail --lines=5 x"),
    ("Bash", "tail -n 5 -F x", "tail -n 5 x"),
    ("Bash", "tail -f -c 100 x", "tail -c 100 x"),
    ("Bash", "tail -20 -f x", "tail -20 x"),
    ("Bash", "tail -n 5 --follow=name x", "tail -n 5 x"),
    ("Bash", "tail -fn 100 x.log", "tail -n 100 x.log"),
    ("Bash", "tail -100f x.log", "tail -100 x.log"),
    ("Bash", f'tail -n 0 -f "{SCR}/s10/probe/run.out" | grep --line-buffered -E "STOP|fine rete|Trace"',
             f'tail -n 0 "{SCR}/s10/probe/run.out" | grep --line-buffered -E "STOP|fine rete|Trace"'),
])
def test_tail_follow_rewritten(mk, tool, cmd, expected):
    assert rewritten(run(mk, cmd, tool)) == expected


@pytest.mark.parametrize("cmd", [
    "tail -F app.log | grep -m1 READY",
    "tail -f x.log | grep -q DONE",
    "tail -f x.log | grep --max-count=1 DONE",
    "tail -f x.log | head -n 5",
    "tail -f x | awk '/READY/ {print; exit}'",
    "tail -f x | sed -n '/READY/{p;q}'",
    "tail -f x | grep --line-buffered y | head -1",
])
def test_follow_into_marker_consumer_blocked(mk, cmd):
    msg = blocked(run(mk, cmd))
    assert "waits for a marker" in msg and "scpy job.py wait <id> --marker <regex>" in msg


@pytest.mark.parametrize("cmd", [
    "tail -f log.txt > mirror.txt 2>&1 &",
    "nohup tail -f x.log &",
    "(tail -f log.txt | grep -m1 DONE) &",
    "(tail -f log.txt | grep -m1 DONE) > out.txt 2>&1 &",
])
def test_backgrounded_follow_left_alone(mk, cmd):
    assert run(mk, cmd) is None


@pytest.mark.parametrize("cmd,expected", [
    ("Get-Content log.txt -Wait", "Get-Content log.txt -Tail 200"),
    ("Get-Content log.txt -Wait -Tail 10", "Get-Content log.txt -Tail 10"),
    ("gc x -wait | Select-String foo", "gc x -Tail 200 | Select-String foo"),
    ("Get-Content -Path x -Wait:$true", "Get-Content -Path x -Tail 200"),
    ("cat x.log -Wait -Last 5", "cat x.log -Last 5"),
    ("Get-Content x -TotalCount 5 -Wait", "Get-Content x -TotalCount 5"),
])
def test_get_content_wait_rewritten(mk, cmd, expected):
    assert rewritten(run(mk, cmd, "PowerShell")) == expected


# Solo le parole `python [-I] -` diventano `true`: separatori, `|| true` e redirezioni restano,
# cosi' la sintassi e lo stato d'uscita non cambiano (`python -` con stdin vuoto esce con 0)
BASH_PY_REWRITES = [
    # forma reale: un `python -` nudo davanti al vero heredoc
    ('F=/c/Users/berni.alberto/code/databricks/reports/gis_proposal/gis_proposal.tex; '
     'python - 2>/dev/null || true; /c/Users/berni.alberto/code/databricks/tools/nb-env/Scripts/python.exe '
     '-I - "$F" <<\'EOF\'\nimport sys\nprint(sys.argv)\nEOF',
     'F=/c/Users/berni.alberto/code/databricks/reports/gis_proposal/gis_proposal.tex; '
     'true 2>/dev/null || true; /c/Users/berni.alberto/code/databricks/tools/nb-env/Scripts/python.exe '
     '-I - "$F" <<\'EOF\'\nimport sys\nprint(sys.argv)\nEOF'),
    ("python - 2>/dev/null || true; python -I - <<'EOF'\nprint(1)\nEOF",
     "true 2>/dev/null || true; python -I - <<'EOF'\nprint(1)\nEOF"),
    ("cd /c/x && python -", "cd /c/x && true"),
    ("python -", "true"),
    ("py -", "true"),
    ("python - || true", "true || true"),
    ('"C:/Python312/python.exe" -I -; ls', "true; ls"),
    ("python3 - 2> /dev/null; echo ok", "true 2> /dev/null; echo ok"),
    ("X=1 python - && ls", "X=1 true && ls"),
    ("( a ); python -", "( a ); true"),
    ("tail -f a.log; python -", "tail -n 200 a.log; true"),
    ("if [ -f x ]; then\n  python -\nfi", "if [ -f x ]; then\n  true\nfi"),
    ("for f in a b; do\n  python -\ndone", "for f in a b; do\n  true\ndone"),
    ("f() {\n  python -\n}", "f() {\n  true\n}"),
    ("{\n  python -\n}", "{\n  true\n}"),
    ("(\n  python -\n)", "(\n  true\n)"),
    ("x && python - 2>/dev/null || true", "x && true 2>/dev/null || true"),
    ("x || python - || true", "x || true || true"),
    ("python - > out.txt", "true > out.txt"),
    ("if python -; then :; fi", "if true; then :; fi"),
    ("python - | tee out", "true | tee out"),
    ("python - || echo failed", "true || echo failed"),
    ("python - &", "true &"),
]


@pytest.mark.parametrize("cmd,expected", BASH_PY_REWRITES)
def test_bare_python_replaced_by_true(mk, cmd, expected):
    assert rewritten(run(mk, cmd)) == expected


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash non disponibile")
@pytest.mark.parametrize("cmd,expected", BASH_PY_REWRITES)
def test_bare_python_rewrite_stays_valid_bash(mk, cmd, expected):
    proc = subprocess.run([shutil.which("bash"), "-n"], input=rewritten(run(mk, cmd)),
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("cmd,expected", [
    ("python -", "$null"),
    ("python - 2>&1", "$null 2>&1"),
    ('& "C:/Python312/python.exe" -', "$null"),
    ("Get-Date; python -I -", "Get-Date; $null"),
])
def test_bare_python_powershell_becomes_null(mk, cmd, expected):
    assert rewritten(run(mk, cmd, "PowerShell")) == expected


@pytest.mark.parametrize("cmd", [
    "python - <<'EOF'\nimport time\ntime.sleep(60)\nwhile True:\n    pass\nEOF",
    "cat f | python -",
    "python -I - \"$F\" <<'EOF'\nprint(1)\nEOF",
    "python - < script.py",
    'python - <<< "print(1)"',
    'python -c "print(1)"',
    "python script.py -",
    "python - arg",
])
def test_python_with_stdin_untouched(mk, cmd):
    assert run(mk, cmd) is None


def test_rewrite_keeps_other_fields_and_says_so(mk):
    res = run(mk, "tail -f x.log", description="show log", timeout=60000)
    assert res.updated_input == {"command": "tail -n 200 x.log", "description": "show log", "timeout": 60000}
    note = "\n".join(res.context)
    assert note.startswith("wait guard: rewrote the command so it returns")
    assert "`tail -f` -> `tail` without the follow flag" in note and "differs from your draft" in note


@pytest.mark.parametrize("tool,cmd", [
    ("Bash", "tail -n 50 log"), ("Bash", 'grep -n "tail -f" f'), ("Bash", "timeout 10 tail -f x.log"),
    ("PowerShell", "Get-Content x -Tail 50"), ("PowerShell", "Get-Content x -Wait:$false"),
    ("Bash", "cat x -Wait"),
])
def test_non_follow_reads_untouched(mk, tool, cmd):
    assert run(mk, cmd, tool) is None


# --------------------------------------------------------------------------
# Testo, heredoc, commenti: mai un riscontro
# --------------------------------------------------------------------------
@pytest.mark.parametrize("tool,cmd", [
    ("Bash", 'echo "sleep 99"'),
    ("Bash", "ls # sleep 60"),
    ("Bash", "printf 'until x; do sleep 60; done'"),
    ("Bash", 'git commit -m "wait: sleep 60 and tail -f removed"'),
    ("Bash", "cat <<'EOF' > s.sh\nwhile true; do sleep 60; done\nfind / -name x\nEOF\nbash -n s.sh"),
    ("Bash", "cat <<-EOF > s.sh\n\twatch ls\n\tEOF\necho ok"),
    ("Bash", 'python -c "import time; time.sleep(60)"'),
    # attesa dentro una stringa Python: limite noto, la guardia non legge il codice Python
    ("Bash", "cd /c/Users/berni.alberto/code/ExcITA && .venv/Scripts/python.exe -c \"\nimport time, pathlib\n"
             "p = pathlib.Path('work/tmp_v4/fix_run.log')\nwhile '=== END' not in p.read_text():\n"
             "    time.sleep(10)\n\""),
    ("PowerShell", "Write-Output 'Start-Sleep 60'"),
])
def test_text_heredoc_and_comments_never_match(mk, tool, cmd):
    assert run(mk, cmd, tool) is None


# Forma standard dei commit: heredoc dentro `"$( ... )"`. Un numero dispari di `"` nel corpo
# non deve invertire le virgolette e far leggere le righe del messaggio come comandi.
COMMIT_ODD_QUOTES = (
    "git commit -m \"$(cat <<'EOF'\nfix: retry logic\n\nUse a 5\" timeout.\n"
    "sleep 30 between retries was too long; tail -f was replaced.\nwatch out for find / scans.\n\n"
    "Co-Authored-By: Claude <noreply@anthropic.com>\nEOF\n)\""
)
COMMIT_BALANCED_QUOTES = (
    "git commit -m \"$(cat <<'EOF'\nfeat: rewrite \"tail -f\" to \"tail -n 200\"\n\nsleep 60 is blocked.\nEOF\n)\""
)


@pytest.mark.parametrize("cmd", [COMMIT_ODD_QUOTES, COMMIT_BALANCED_QUOTES])
def test_commit_heredoc_in_command_substitution_never_matches(mk, cmd):
    assert run(mk, cmd) is None


def test_command_substitution_inside_double_quotes_is_code(mk):
    assert "waits 60 s" in blocked(run(mk, 'echo "$(sleep 60)"'))
    assert rewritten(run(mk, 'x="$(tail -f log)"; echo "$x"')) == 'x="$(tail -n 200 log)"; echo "$x"'


# --------------------------------------------------------------------------
# Eccezioni: job.py, background, run_in_background
# --------------------------------------------------------------------------
@pytest.mark.parametrize("cmd", [
    "scpy job.py wait j1 --max 540",
    "scpy job.py start --name j1 -- bash -c \"sleep 60\"",
    "python C:/Users/x/.claude/scripts/job.py wait j1 --max 540 --stall 300",
])
def test_job_helper_is_exempt(mk, cmd):
    assert run(mk, cmd) is None


@pytest.mark.parametrize("cmd", [
    "(sleep 300; kill $PID) &",
    "sleep 60 &",
    "sleep 30 && kill $p &",
    "{ sleep 120; touch done; } &",
    "while true; do date; sleep 5; done > log &",
    "./serve.sh & pid=$!; (sleep 45; kill $pid) & wait $pid",
    # redirezioni tra la parentesi che chiude e `&`: il gruppo resta in background
    "(while true; do sleep 60; done) > /dev/null 2>&1 &",
    "( sleep 300; kill $p ) >/dev/null 2>&1 &",
    "{ sleep 120; touch done; } &>/dev/null &",
    "(sleep 90; kill $p) 2>>err.log < /dev/null &",
])
def test_background_watchdogs_pass(mk, cmd):
    assert run(mk, cmd) is None


@pytest.mark.parametrize("cmd", ["(sleep 60) > log", "( sleep 60 ) > log && echo ok"])
def test_redirected_group_in_foreground_still_blocked(mk, cmd):
    blocked(run(mk, cmd))


@pytest.mark.parametrize("role", ["meta", "plain"])
def test_background_launch_by_meta_may_poll(mk, role):
    assert run(mk, "until grep -q DONE log; do sleep 10; done", role=role, run_in_background=True) is None
    assert run(mk, "sleep 120; echo late", role=role, run_in_background=True) is None
    # lanciato in background il comando parte com'e': niente riscritture
    assert run(mk, "tail -f x.log", role=role, run_in_background=True) is None
    assert run(mk, "python - 2>/dev/null || true; ls", role=role, run_in_background=True) is None


def test_background_launch_by_worker_still_blocked(mk):
    blocked(run(mk, "until grep -q DONE log; do sleep 10; done", role="worker", run_in_background=True))
    assert rewritten(run(mk, "tail -f x.log", role="worker", run_in_background=True)) == "tail -n 200 x.log"


def test_background_does_not_excuse_a_wide_find(mk):
    blocked(run(mk, "find / -name x", role="meta", run_in_background=True))


# --------------------------------------------------------------------------
# Monitor
# --------------------------------------------------------------------------
@pytest.mark.parametrize("cmd", [
    "until grep -q LLMDONE out.txt 2>/dev/null; do sleep 5; done",
    f'tail -n 0 -f "{SCR}/s10/probe/run.out" | grep --line-buffered -E "STOP|fine rete"',
    "echo hi",
])
@pytest.mark.parametrize("agent", ["w-implementer", "w-tester", "W-Doc", "general-purpose", "Explore"])
def test_monitor_denied_to_every_subagent(mk, cmd, agent):
    msg = blocked(run(mk, cmd, "Monitor", role="worker", agent=agent))
    assert "reserved for the main session (meta)" in msg and "hand back with the exact launch command" in msg


@pytest.mark.parametrize("role,agent", [("meta", "meta"), ("plain", "")])
def test_monitor_free_for_the_main_session(mk, role, agent):
    cmd = "until grep -q DONE out.txt; do sleep 5; done"
    assert run(mk, cmd, "Monitor", role=role, agent=agent) is None


@pytest.mark.parametrize("payload,denied", [
    ({"agent_type": "general-purpose"}, True), ({"agent_type": "w-implementer"}, True),
    ({"agent_type": "meta"}, False), ({}, False),
])
def test_monitor_role_comes_from_resolve_identity(mk, monkeypatch, payload, denied):
    monkeypatch.delenv("CLAUDE_AGENT_NAME", raising=False)
    role, agent = core.resolve_identity(payload)
    res = run(mk, "echo hi", "Monitor", role=role, agent=agent)
    assert (res is not None and bool(res.block)) is denied


# --------------------------------------------------------------------------
# Dispatcher: etichetta, riscrittura, modalita', fail-open
# --------------------------------------------------------------------------
@pytest.fixture
def only_wait(monkeypatch):
    entries = [e for e in core._REGISTRY if e[2] == wait_guard.NAME and e[3] is wait_guard.handle]
    assert entries, "wait guard non registrata"
    monkeypatch.setattr(core, "_REGISTRY", entries[:1])


def test_registered_on_every_shell_tool():
    found = [(ev, tools) for ev, tools, name, fn in core._REGISTRY if name == wait_guard.NAME]
    assert found and all(ev == "PreToolUse" and set(tools) == set(core.SHELL_TOOLS) for ev, tools in found)


def test_dispatch_block_carries_tag_and_replacement(mk, only_wait, capsys):
    assert core.dispatch(mk("Bash", command="until test -f x; do sleep 10; done")) == 2
    err = capsys.readouterr().err
    assert err.startswith("GUARD-BLOCK [wait] ")
    assert "scpy job.py wait <id> --max 540" in err


def test_dispatch_rewrite_emits_updated_input(mk, only_wait, capsys):
    assert core.dispatch(mk("Bash", command="tail -f x.log")) == 0
    out = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert out["updatedInput"]["command"] == "tail -n 200 x.log"
    assert "wait guard: rewrote" in out["additionalContext"]


def test_warn_mode_turns_block_into_note(mk, only_wait, capsys, monkeypatch):
    monkeypatch.setenv("SUPERCLAUDE_GUARD_WAIT", "warn")
    assert core.dispatch(mk("Bash", command="sleep 90")) == 0
    assert "GUARD-WARN [wait]" in json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]


def test_internal_error_fails_open(mk, only_wait, capsys, monkeypatch, home):
    def boom(*_a, **_k):
        raise RuntimeError("bug")
    monkeypatch.setattr(wait_guard, "_analyze", boom)
    assert core.dispatch(mk("Bash", command="sleep 90")) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and captured.out == ""
    assert "PreToolUse/wait" in (home / ".claude" / "logs" / "hooks.log").read_text(encoding="utf-8")


@pytest.mark.parametrize("ti", [{}, {"command": None}, {"command": 7}, {"command": ""}, {"command": "   "},
                                {"command": ["sleep", "60"]}])
def test_malformed_input_is_ignored(mk, ti):
    assert wait_guard.handle(mk("Bash", tool_input=ti)) is None


@pytest.mark.parametrize("tool,cmd", [
    ("Bash", 'echo "sleep 60'), ("Bash", "cat <<EOF\nsleep 60"), ("Bash", "echo $(( 1 << 2 ))"),
    ("Bash", "echo 'a\\"), ("Bash", ")))((( done done"), ("Bash", "do; done; until"),
    ("PowerShell", "@'\nStart-Sleep 60"), ("PowerShell", "<# Start-Sleep 60"), ("PowerShell", "while ("),
    ("Bash", "\\"), ("PowerShell", "`"),
])
def test_malformed_commands_do_not_crash(mk, tool, cmd):
    # testo non chiuso o strutture spezzate: nessuna eccezione e nessuna azione
    assert run(mk, cmd, tool) is None


def test_huge_command_is_fast(mk):
    cmd = "echo a; " * 12_000 + "sleep 60"
    start = time.perf_counter()
    res = run(mk, cmd)
    assert time.perf_counter() - start < 3.0
    assert res is not None and res.block


@pytest.mark.parametrize("mode", ["bash", "ps", "cmd"])
@pytest.mark.parametrize("cmd", [
    "a 'b' \"c\" # d\nx", "cat <<'EOF'\nbody\nEOF\nls", "@'\nhere\n'@\nx", "a\\\nb", "a`\nb", '"un', "'un",
    "<# c #> x", "$'a\\'b' c", "x ^& y", 'a "$(b "c" d)" e', '"$(cat <<\'E\'\nx"\nE\n)" z', '"$(unclosed',
])
def test_mask_keeps_positions(cmd, mode):
    assert len(wait_guard._Mask(cmd, mode).run()) == len(cmd)
