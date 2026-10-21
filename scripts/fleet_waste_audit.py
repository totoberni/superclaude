"""Audit dei transcript dei subagent: code post-lavoro (P1), attese (P2) e KPI di igiene.

Uso: python fleet_waste_audit.py --out <dir> [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--root <dir>]
Scrive report.md (tabella KPI + sezioni di dettaglio), runs.csv, calls_shell.csv. Sola lettura sui transcript.
"""
import argparse
import collections
import csv
import glob
import json
import os
import re
import time
from datetime import datetime, timedelta
from multiprocessing import Pool

WRITE_TOOLS = {'Edit', 'Write', 'NotebookEdit', 'MultiEdit'}
SHELL_TOOLS = {'Bash', 'PowerShell'}
END_TOOLS = {'SubagentHandback', 'StructuredOutput'}
GUARD_TOOLS = {'Bash', 'PowerShell', 'Monitor'}
PRODUCER_TYPES = {'w-implementer', 'w-refactorer', 'w-debugger', 'w-doc'}


def _rx(p):
    return re.compile(p, re.I | re.S)


# Classi di comando, in ordine di priorita: la prima che combacia vince
CMD_CLASSES = [
    ('poll-loop', _rx(r'\b(while|until)\b[^\n]*?(sleep|start-sleep)|\bfor\b[^\n]*\b(sleep|start-sleep)\b')),
    ('tail-follow', _rx(r'\btail\s+(-[a-z]*f|--follow)|get-content\b[^|]*-wait\b')),
    ('sleep', _rx(r'(^|[;&|(\s])(sleep\s+\d|start-sleep\b|timeout\s+/t\s+\d)')),
    ('watch', _rx(r'(^|\s)watch\s|--watch\b|\bnodemon\b|\bptw\b|pytest-watch')),
    ('server', _rx(r'http\.server|\buvicorn\b|flask\s+run|streamlit\s+run|jupyter\s+(notebook|lab)\b|npm\s+run\s+(dev|start|serve)|\bnpx\s+(serve|vite)\b|\bvite\b(?!st)|gunicorn|python\s+-m\s+http')),
    ('test-run', _rx(r'\bpytest\b|-m\s+unittest|\bnpm\s+(run\s+)?test\b|\bvitest\b|\bjest\b|cargo\s+test|go\s+test|\bctest\b|\btox\b|python\S*\s+[^\s|;&]*test[^\s|;&]*\.py|invoke-pester|run_tests')),
    ('lint', _rx(r'\bruff\b|\bflake8\b|\bpylint\b|\bmypy\b|\bpyright\b|\beslint\b|\btsc\b|black\s+--check|py_compile|compileall|shellcheck|ast\.parse|chktex|lacheck|json\.tool|node\s+--check|bash\s+-n\b')),
    ('notebook-exec', _rx(r'nbconvert[^\n]*--execute|papermill|nb_run|nbclient|execute_notebook')),
    ('build', _rx(r'latexmk|pdflatex|xelatex|lualatex|bibtex|biber|\bmake\b|\bcmake\b|npm\s+run\s+build|cargo\s+build|quarto\s+render|\bpandoc\b|\bmsbuild\b|docker\s+build')),
    ('git-write', _rx(r'\bgit\b[^\n]*?\b(commit|add|push|stash|checkout|reset|merge|rebase|restore|cherry-pick|worktree\s+add|apply)\b')),
    ('git-read', _rx(r'\bgit\b')),
    ('mem', _rx(r'(^|[;&|\s])mem\s+(search|get|list|similar|stats|upsert)')),
    ('network', _rx(r'\bcurl\b|\bwget\b|invoke-webrequest|invoke-restmethod|requests\.get|urllib')),
    ('install', _rx(r'pip\s+install|npm\s+(install|i)\b|uv\s+(pip|add|sync)|conda\s+install|winget')),
    ('db-query', _rx(r'duckdb|sqlite3|databricks\s|psql|run_sql')),
    ('script-run', _rx(r'\bpython[w0-9.]*(\.exe)?\b|\bpy\s+-|\bscpy\b|\bnode\b|\bRscript\b|\.ps1\b|\.sh\b|\bpwsh\b|\bpowershell\b')),
    ('grep-search', _rx(r'\bgrep\b|\brg\b|findstr|select-string|(^|[;&|\s])find\s')),
    ('read-file', _rx(r'(^|[;&|\s])(cat|head|tail|less|more|wc|type|sed\s+-n|awk)\s|get-content')),
    ('fs-list', _rx(r'(^|[;&|\s])(ls|dir|get-childitem|gci|tree|stat|du|test\s+-[ef])\b')),
]

# Raggruppamento delle classi per il costo dei turni di coda
GROUP = {}
for _names, _grp in (
    (('test-run',), 'test'),
    (('script-adhoc', 'lint', 'grep-search', 'read', 'read-file', 'git-read', 'fs-list', 'mem'), 'verify'),
    (('script-run', 'db-query', 'build', 'notebook-exec', 'network', 'install', 'web', 'other-shell'), 'product-run'),
    (('poll-loop', 'sleep', 'monitor', 'tail-follow', 'watch', 'poll-output', 'server', 'taskstop'), 'wait'),
    (('checkpoint-write', 'scratch-write', 'edit', 'git-write'), 'write/git'),
):
    for _n in _names:
        GROUP[_n] = _grp

WAIT_CLASSES = ('sleep', 'poll-loop', 'tail-follow', 'watch', 'monitor', 'poll-output')
VERIFY_CLASSES = {'test-run', 'lint', 'grep-search', 'read', 'read-file', 'git-read', 'script-run', 'build', 'notebook-exec', 'fs-list'}

ADHOC_RX = _rx(r"""python[\w.]*(\.exe)?["']?\s+(-I\s+|-X\s*\S+\s+)*(-c\s|-\s*(-\S+\s*)*<<|-\s+\S*\s*<<)|scratchpad|/tmp/|\\tmp\\|\.claude/tmp""")
TARGETED_RX = _rx(r'\.py\b|::|\s-k\s|--lf\b|--last-failed|\s-m\s+(?!pytest|unittest)["\']?\w|unittest\s+[\w.]*test\w*|\.test\.|\.spec\.|vitest\s+run\s+\S+\.')
TASKID_RX = re.compile(r'<task-id>([\w-]+)</task-id>')
STATUS_RX = re.compile(r'<status>(\w+)</status>')
STATUSTOK_RX = re.compile(r'\b(STATUS|VERDICT)\s*:\s*\**\s*([A-Z_-]+)')
# Forma reale di una negazione: il risultato INIZIA con il wrapper dell'hook (non citazioni in prompt o stdout)
GUARD_WAIT_RX = re.compile(r'\s*PreToolUse:\w+ hook error: \[[^\]]*\]: GUARD-BLOCK\s*\[wait\]')
JOB_PY_RX = re.compile(r'\bjob\.py\b')


def classify_cmd(cmd):
    for name, rx in CMD_CLASSES:
        if rx.search(cmd):
            return name
    return 'other-shell'


def test_scope(cmd):
    return 'targeted' if TARGETED_RX.search(cmd) else 'full-suite'


def tool_class(name, inp):
    if name in SHELL_TOOLS:
        cmd = inp.get('command', '') or ''
        k = classify_cmd(cmd)
        return 'script-adhoc' if k == 'script-run' and ADHOC_RX.search(cmd) else k
    return {
        'Read': 'read', 'Grep': 'grep-search', 'Glob': 'grep-search', 'Edit': 'edit', 'Write': 'edit',
        'NotebookEdit': 'edit', 'MultiEdit': 'edit', 'WebFetch': 'web', 'WebSearch': 'web', 'Monitor': 'monitor',
        'Skill': 'skill', 'ToolSearch': 'toolsearch', 'SendMessage': 'comms', 'TaskStop': 'taskstop',
        'TaskOutput': 'poll-output', 'BashOutput': 'poll-output', 'SubagentHandback': 'handback',
        'StructuredOutput': 'handback',
    }.get(name, 'tool:' + name)


def ts_parse(s):
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()
    except Exception:
        return None


def is_aux_path(p):
    q = (p or '').replace('\\', '/').lower()
    if 'checkpoint' in q or q.endswith(('report.md', 'outcome.md', 'outcomes.md')) or '/comms/' in q:
        return 'checkpoint'
    if '/scratchpad/' in q or '/tmp/' in q and '/.claude/tmp/' not in q:
        return 'scratch'
    return None


def text_of(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''
    out = []
    for b in content:
        if isinstance(b, dict) and b.get('type') == 'text':
            out.append(b.get('text', ''))
        elif isinstance(b, dict) and b.get('type') == 'tool_result':
            out.append(text_of(b.get('content')))
    return '\n'.join(out)


def esc(s):
    return (s or '').replace('|', '\\|').replace('\n', ' ').replace('\r', ' ').replace('`', "'")


def pct(xs, q):
    if not xs:
        return 0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))]


# ---------------------------------------------------------------------------
# Lettura di un transcript (streaming riga per riga)
# ---------------------------------------------------------------------------

class TranscriptScan:
    """Stato incrementale di un transcript: chiamate, gap di attesa, notifiche."""

    def __init__(self):
        self.calls, self.order, self.pending = {}, [], set()
        self.msg_ids, self.models = set(), collections.Counter()
        self.first_ts = self.first_tsv = self.last_ts = self.last_asst_ts = None
        self.last_text = self.final_text = self.status = ''
        self.gaps, self.notif, self.seen_keys = [], {}, set()

    def feed(self, d):
        t = d.get('type')
        tsv = ts_parse(d['timestamp']) if d.get('timestamp') else None
        if t not in ('assistant', 'user', 'attachment') or tsv is None:
            return
        if self.first_ts is None:
            self.first_ts, self.first_tsv = d['timestamp'], tsv
        had_pending = bool(self.pending)
        if t == 'attachment':
            self._attachment(d, tsv, had_pending)
        elif t == 'assistant':
            self._assistant(d, tsv)
        else:
            self._user(d, tsv, had_pending)
        self.last_ts = tsv

    def _note_notif(self, text, tsv):
        st = STATUS_RX.search(text)
        for tid in TASKID_RX.findall(text):
            self.notif.setdefault(tid, (tsv, st.group(1) if st else ''))

    def _note_gap(self, kind, key_text, tsv, ts_s, had_pending):
        key = (kind, key_text[:200])
        if key in self.seen_keys:
            return
        self.seen_keys.add(key)
        if not had_pending and self.last_asst_ts is not None and tsv - self.last_asst_ts > 5:
            self.gaps.append((kind, tsv - self.last_asst_ts, ts_s))

    def _attachment(self, d, tsv, had_pending):
        a = d.get('attachment') or {}
        if a.get('type') != 'queued_command':
            return
        pr = a.get('prompt') or ''
        if not isinstance(pr, str):
            pr = text_of(pr)
        kind = 'notif' if '<task-notification>' in pr else 'queued-msg'
        self._note_notif(pr, tsv)
        self._note_gap(kind, pr, tsv, d['timestamp'], had_pending)

    def _assistant(self, d, tsv):
        m = d.get('message') or {}
        if m.get('id'):
            self.msg_ids.add(m['id'])
        if m.get('model') and not str(m['model']).startswith('<'):
            self.models[m['model']] += 1
        for b in m.get('content') if isinstance(m.get('content'), list) else []:
            if not isinstance(b, dict):
                continue
            if b.get('type') == 'text' and b.get('text', '').strip():
                self.last_text = self.final_text = b['text']
            elif b.get('type') == 'tool_use':
                self._tool_use(b, tsv, d['timestamp'])
        self.last_asst_ts = tsv

    def _tool_use(self, b, tsv, ts_s):
        inp = b.get('input') if isinstance(b.get('input'), dict) else {}
        self.calls[b.get('id')] = {'id': b.get('id'), 'name': b.get('name'), 'input': inp, 'ts': tsv, 'ts_s': ts_s,
                                   'before': self.last_text[-300:], 'idx': len(self.order)}
        self.order.append(b.get('id'))
        self.pending.add(b.get('id'))
        if b.get('name') in END_TOOLS:
            msg = inp.get('message') or json.dumps(inp)
            msg = msg if isinstance(msg, str) else str(msg)
            mm = STATUSTOK_RX.search(msg)
            if mm:
                self.status = mm.group(1) + ':' + mm.group(2)
            elif b.get('name') == 'StructuredOutput' and inp.get('status'):
                self.status = 'STATUS:' + str(inp.get('status'))
            self.final_text = msg
        self.last_text = ''

    def _user(self, d, tsv, had_pending):
        m = d.get('message') or {}
        content = m.get('content')
        tur = d.get('toolUseResult')
        results = [b for b in content if isinstance(b, dict) and b.get('type') == 'tool_result'] if isinstance(content, list) else []
        for b in results:
            self._tool_result(b, d, tur, tsv)
        if results:
            return
        txt = text_of(content)
        if '<task-notification>' in txt:
            kind = 'notif'
            self._note_notif(txt, tsv)
        elif txt.startswith(('Base directory for this skill', '<command-', '<system-reminder>')):
            return
        else:
            kind = 'user-msg'
        self._note_gap(kind, txt, tsv, d['timestamp'], had_pending)

    def _tool_result(self, b, d, tur, tsv):
        c = self.calls.get(b.get('tool_use_id'))
        if c is None:
            return
        self.pending.discard(b.get('tool_use_id'))
        c['res_ts'] = tsv
        c['is_error'] = bool(b.get('is_error'))
        c['perm_user'] = str((d.get('permissionDecision') or {}).get('source', '')).startswith('user')
        txt = text_of(b.get('content'))
        c['res_head'] = txt[:400]
        low = txt[:3000].lower()
        c['timeout_txt'] = ('timed out' in low or 'timeout' in low and ('exceeded' in low or 'killed' in low))
        c['guard_wait'] = c['name'] in GUARD_TOOLS and bool(GUARD_WAIT_RX.match(txt[:600]))
        if isinstance(tur, dict):
            c['bg_id'] = tur.get('backgroundTaskId')
            c['timed_out_ms'] = tur.get('timedOutAfterMs')
            c['interrupted'] = tur.get('interrupted')
            c['bash_edit'] = bool(tur.get('bashEditDiff'))


def process(path):
    meta = {}
    try:
        with open(path[:-6] + '.meta.json', encoding='utf-8', errors='replace') as fh:
            meta = json.load(fh)
    except Exception:
        pass
    sc = TranscriptScan()
    nline = 0
    with open(path, encoding='utf-8', errors='replace') as fh:
        for line in fh:
            nline += 1
            if '"api-request' in line[:300]:
                continue
            try:
                sc.feed(json.loads(line))
            except Exception:
                continue
    if sc.first_ts is None:
        return None
    return {'path': path.replace('\\', '/'), 'meta': meta, 'calls': [sc.calls[i] for i in sc.order], 'turns': len(sc.msg_ids),
            'models': sc.models.most_common(1)[0][0] if sc.models else '', 'first_ts': sc.first_ts, 'first_tsv': sc.first_tsv,
            'last_tsv': sc.last_ts, 'status': sc.status, 'final_text': (sc.final_text or '')[-600:], 'gaps': sc.gaps,
            'notif': sc.notif, 'nline': nline}


# ---------------------------------------------------------------------------
# Analisi per run
# ---------------------------------------------------------------------------

def annotate_calls(calls):
    for c in calls:
        c['cls'] = tool_class(c['name'], c['input'])
        c['dur'] = (c['res_ts'] - c['ts']) if c.get('res_ts') is not None else None
        c['cmd'] = (c['input'].get('command') or '') if c['name'] in SHELL_TOOLS else ''
        p = c['input'].get('file_path') or c['input'].get('notebook_path') or ''
        aux = is_aux_path(p) if c['name'] in WRITE_TOOLS else None
        c['work_edit'] = (c['name'] in WRITE_TOOLS and aux is None) or bool(c.get('bash_edit'))
        if c['name'] in WRITE_TOOLS and aux:
            c['cls'] = aux + '-write'


def tail_kind(tcalls, ntest, nfull, nverify):
    if not tcalls:
        return 'none'
    if nfull >= 1 or ntest >= 2 or (len(tcalls) >= 6 and nverify >= 4):
        return 'heavy'
    if len(tcalls) <= 2 and ntest <= 1:
        return 'interim-check'
    return 'moderate'


def tail_cost(tcalls, start):
    """Costo per gruppo = latenza del modello prima della chiamata + attesa dello strumento; gap > 600 s sono idle."""
    cost = collections.Counter()
    prev_end = start
    for c in tcalls:
        end_c = c.get('res_ts') or c['ts']
        if c.get('perm_user'):
            pass
        elif c['ts'] - prev_end > 600:
            cost[GROUP.get(c['cls'], 'other')] += (c['dur'] or 0) / 60
        else:
            cost[GROUP.get(c['cls'], 'other')] += max(0.0, end_c - prev_end) / 60
        prev_end = end_c
    return dict(cost)


def analyse_tail(r, calls, widx):
    li = widx[-1]
    start = calls[li].get('res_ts') or calls[li]['ts']
    after = calls[li + 1:]
    cut = next((k for k, c in enumerate(after) if c['name'] in END_TOOLS), len(after))
    tcalls = [c for c in after[:cut] if c['name'] != 'SendMessage']
    elapsed = 0.0
    if tcalls:
        end = max((c.get('res_ts') or c['ts']) for c in tcalls)
        idle_in = sum(g[1] for g in r['gaps'] if g[0] in ('user-msg', 'queued-msg', 'notif') and start <= ts_parse(g[2]) <= end)
        perm_in = sum((c['dur'] or 0) for c in tcalls if c.get('perm_user'))
        elapsed = max(0.0, (end - start - idle_in - perm_in) / 60)
    bycls, bycls_min = collections.Counter(), collections.Counter()
    for c in tcalls:
        bycls[c['cls']] += 1
        bycls_min[c['cls']] += (c['dur'] or 0) / 60
    ntest = bycls['test-run']
    nfull = sum(1 for c in tcalls if c['cls'] == 'test-run' and test_scope(c['cmd']) == 'full-suite')
    nverify = sum(v for k, v in bycls.items() if k in VERIFY_CLASSES)
    return {'n': len(tcalls), 'cost': tail_cost(tcalls, start), 'nverify': sum(1 for c in tcalls if GROUP.get(c['cls']) == 'verify'),
            'elapsed_min': elapsed, 'tool_min': sum((c['dur'] or 0) for c in tcalls if not c.get('perm_user')) / 60,
            'bycls': dict(bycls), 'bycls_min': {k: round(v, 2) for k, v in bycls_min.items()},
            'kind': tail_kind(tcalls, ntest, nfull, nverify), 'ntest': ntest, 'nfull': nfull, 'last_edit_idx': li, 'ncalls': len(calls)}


def analyse_bg(r, calls):
    bg = []
    for i, c in enumerate(calls):
        tid = c.get('bg_id')
        if not tid:
            continue
        later = calls[i + 1:]
        polls = [x for x in later if x['name'] == 'Monitor' or x['cls'] in ('sleep', 'poll-loop', 'tail-follow', 'poll-output')
                 or tid in json.dumps(x['input']) or ('.output' in json.dumps(x['input']) and x['name'] in ('Read', 'Bash', 'PowerShell'))]
        n = r['notif'].get(tid)
        stopped = any(x['name'] == 'TaskStop' and tid in json.dumps(x['input']) for x in later)
        bg.append({'tid': tid, 'auto': bool(c.get('timed_out_ms')), 'explicit': bool(c['input'].get('run_in_background')),
                   'cmd': c['cmd'][:200], 'cls': c['cls'], 'npolls': len(polls), 'poll_min': sum((x['dur'] or 0) for x in polls) / 60,
                   'notif_after_min': ((n[0] - c['ts']) / 60) if n else None, 'notif_status': n[1] if n else '',
                   'stopped': stopped, 'finished': bool(n) or stopped})
    return bg


def post_handback_calls(calls):
    """Chiamate dopo l'ULTIMO handback: l'agente ha riferito in via definitiva ma tiene strumenti/shell aperti.
    Gli handback intermedi di run ripresi con SendMessage non contano."""
    last = max((k for k, c in enumerate(calls) if c['name'] in END_TOOLS), default=None)
    if last is None:
        return 0
    return sum(1 for c in calls[last + 1:] if c['name'] not in END_TOOLS)


def analyse(r):
    calls = r['calls']
    at = r['meta'].get('agentType') or ('workflow' if '/workflows/' in r['path'] else '?')
    if '/workflows/' in r['path'] and at != 'workflow':
        at += '[wf]'
    annotate_calls(calls)
    wall = (r['last_tsv'] - r['first_tsv']) / 60
    idle_resume = sum(g[1] for g in r['gaps'] if g[0] in ('user-msg', 'queued-msg')) / 60
    widx = [i for i, c in enumerate(calls) if c['work_edit']]
    return {'at': at, 'wall': wall, 'active': wall - idle_resume, 'idle_resume': idle_resume,
            'bg_wait': sum(g[1] for g in r['gaps'] if g[0] == 'notif') / 60,
            'tool_min': sum(c['dur'] or 0 for c in calls) / 60, 'tail': analyse_tail(r, calls, widx) if widx else None,
            'bg': analyse_bg(r, calls), 'post_handback': post_handback_calls(calls)}


# ---------------------------------------------------------------------------
# Raccolta dei file nella finestra
# ---------------------------------------------------------------------------

def window_bounds(since, until):
    start = since + 'T00:00:00'
    end = (datetime.strptime(until, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%dT00:00:00')
    return start, end


def candidate_files(root, since):
    """Un transcript modificato prima di 'since' non puo' iniziare dopo: scarto per mtime, con un giorno di margine."""
    floor = datetime.strptime(since, '%Y-%m-%d').timestamp() - 86400
    files = glob.glob(root + '/*/*/subagents/**/agent-*.jsonl', recursive=True)
    return files, [f for f in files if os.path.getmtime(f) >= floor]


def parent_waiting_ids(root, since):
    """Conta gli avvisi 'waiting on its own background work' nei transcript delle sessioni principali."""
    floor = datetime.strptime(since, '%Y-%m-%d').timestamp() - 86400
    ids = collections.Counter()
    for f in glob.glob(root + '/*/*.jsonl'):
        try:
            if os.path.getmtime(f) < floor:
                continue
            with open(f, encoding='utf-8', errors='replace') as fh:
                for line in fh:
                    if 'waiting on its own background work' in line:
                        ids.update(TASKID_RX.findall(line.replace('\\n', '\n')))
        except Exception:
            pass
    return ids


def collect(root, since, until):
    start, end = window_bounds(since, until)
    all_files, cand = candidate_files(root, since)
    with Pool(8) as pool:
        results = [x for x in pool.imap_unordered(process, cand, chunksize=4) if x]
    results = [r for r in results if start <= r['first_ts'] < end]
    waiting = parent_waiting_ids(root, since)
    runs, allcalls = [], []
    for r in results:
        r['a'] = analyse(r)
        r['aid'] = os.path.basename(r['path'])[6:-6]
        r['parent_waiting_notice'] = waiting.get(r['aid'], 0)
        runs.append(r)
        allcalls.extend((r, c) for c in r['calls'])
    return runs, allcalls, len(all_files), len(cand)


# ---------------------------------------------------------------------------
# Tabella KPI
# ---------------------------------------------------------------------------

def wait_breakdown(runs, allcalls):
    """Minuti di attesa pura per tipo: poll/sleep/monitor, comandi scaduti poi in background, idle fino alla notifica."""
    wt = collections.defaultdict(lambda: [0, 0.0])
    for r, c in allcalls:
        if c.get('perm_user'):
            continue
        key = c['cls'] if c['cls'] in WAIT_CLASSES else ('timed-out then auto-bg: ' + c['cls'] if c.get('timed_out_ms') else None)
        if key:
            wt[key][0] += 1
            wt[key][1] += (c['dur'] or 0) / 60
    for r in runs:
        for g in r['gaps']:
            if g[0] == 'notif':
                wt['idle until bg notification'][0] += 1
                wt['idle until bg notification'][1] += g[1] / 60
    return wt


def is_producer(at):
    return at.replace('[wf]', '') in PRODUCER_TYPES


def per100(n, base):
    return f'{100 * n / base:.1f}' if base else 'n/a'


def kpi_rows(runs, allcalls):
    active = sum(r['a']['active'] for r in runs)
    prod = [r for r in runs if is_producer(r['a']['at'])]
    prod_full = [r for r in prod if r['a']['tail'] and r['a']['tail']['nfull'] >= 1]
    tail_min = sum(r['a']['tail']['elapsed_min'] for r in runs if r['a']['tail'])
    wait_min = sum(v[1] for v in wait_breakdown(runs, allcalls).values())
    kept = [r for r in runs if r['a']['post_handback'] > 0]
    unfinished = [r for r in runs if any(not b['finished'] for b in r['a']['bg'])]
    auto_bg = [c for _, c in allcalls if c.get('timed_out_ms')]
    permw = [c for _, c in allcalls if c.get('perm_user')]
    guard = [c for _, c in allcalls if c.get('guard_wait')]
    jobpy = [c for _, c in allcalls if c['name'] in SHELL_TOOLS and JOB_PY_RX.search(c['cmd'])]
    return [
        ('Runs (subagent transcripts in window)', len(runs), f'active minutes total {active:.0f}'),
        ('Producer runs (w-implementer, w-refactorer, w-debugger, w-doc, incl. [wf])', len(prod), ''),
        ('Producer runs with a full-suite test run after the last edit', len(prod_full), f'{per100(len(prod_full), len(prod))} per 100 producer runs'),
        ('Post-work tail minutes', f'{tail_min:.0f}', f'{per100(tail_min, active)} per 100 active minutes'),
        ('Pure-wait minutes', f'{wait_min:.0f}', f'{per100(wait_min, active)} per 100 active minutes'),
        ('Runs that kept calling tools after SubagentHandback', len(kept), f'{sum(r["a"]["post_handback"] for r in kept)} calls'),
        ('Runs ending with a background shell never finished or stopped', len(unfinished), ''),
        ('w-tester runs (incl. [wf])', sum(1 for r in runs if r['a']['at'].replace('[wf]', '') == 'w-tester'), ''),
        ('Commands that timed out then were auto-backgrounded', len(auto_bg), ''),
        ('Permission-prompt wait minutes', f'{sum((c["dur"] or 0) for c in permw) / 60:.0f}', f'{len(permw)} calls (approval wait plus execution)'),
        ('GUARD-BLOCK [wait] denials', len(guard), 'new tooling, zero before rollout'),
        ('job.py calls', len(jobpy), 'new tooling, zero before rollout'),
    ]


def section_kpi(runs, allcalls, since, until):
    out = [f'# Fleet waste audit {since} .. {until}\n', '## KPI\n', '| metric | value | note |', '|---|---|---|']
    out += [f'| {m} | {v} | {n} |' for m, v, n in kpi_rows(runs, allcalls)]
    out.append('')
    return out


# ---------------------------------------------------------------------------
# Sezioni di dettaglio (stesse del miner)
# ---------------------------------------------------------------------------

def group_by_type(runs):
    by_at = collections.defaultdict(list)
    for r in runs:
        by_at[r['a']['at']].append(r)
    return by_at


def section_window(runs, allcalls, n_files, n_cand, since, until):
    firsts = sorted(r['first_ts'] for r in runs) or ['-']
    return ['## Data window\n',
            f'- Transcript files found: {n_files} ({n_cand} touched since {since}); in window ({since} .. {until}): **{len(runs)}** runs; first-ts range {firsts[0][:16]} .. {firsts[-1][:16]} UTC.',
            f'- Tool calls analysed: {len(allcalls)}. Wall = first to last timestamp; active = wall minus idle gaps that ended with a SendMessage/queued message. Tool wall = tool_result ts minus tool_use ts.', '']


def section_types(by_at):
    out = ['## 1. Runs per agentType\n',
           '| agentType | runs | wall med | wall p90 | active med | active p90 | turns med | turns p90 | tool-min total | resume-idle min total | bg-wait idle min total |',
           '|---|---|---|---|---|---|---|---|---|---|---|']
    for at, rs in sorted(by_at.items(), key=lambda kv: -len(kv[1])):
        w = [r['a']['wall'] for r in rs]
        ac = [r['a']['active'] for r in rs]
        tu = [r['turns'] for r in rs]
        out.append(f"| {at} | {len(rs)} | {pct(w, .5):.1f} | {pct(w, .9):.1f} | {pct(ac, .5):.1f} | {pct(ac, .9):.1f} | {pct(tu, .5)} | {pct(tu, .9)} | "
                   f"{sum(r['a']['tool_min'] for r in rs):.0f} | {sum(r['a']['idle_resume'] for r in rs):.0f} | {sum(r['a']['bg_wait'] for r in rs):.0f} |")
    out += ['', '### Per model (types with more than one model)\n', '| agentType | model | runs | active med | active p90 | turns med |', '|---|---|---|---|---|---|']
    for at, rs in sorted(by_at.items()):
        bym = collections.defaultdict(list)
        for r in rs:
            bym[r['models'] or '?'].append(r)
        if len(bym) > 1:
            for mdl, rr in sorted(bym.items(), key=lambda kv: -len(kv[1])):
                ac = [r['a']['active'] for r in rr]
                out.append(f"| {at} | {mdl} | {len(rr)} | {pct(ac, .5):.1f} | {pct(ac, .9):.1f} | {pct([r['turns'] for r in rr], .5)} |")
    return out + ['']


def section_histogram(by_at, allcalls):
    cls_at, cls_at_min = collections.defaultdict(collections.Counter), collections.defaultdict(collections.Counter)
    tot, totm = collections.Counter(), collections.Counter()
    for r, c in allcalls:
        cls_at[r['a']['at']][c['cls']] += 1
        cls_at_min[r['a']['at']][c['cls']] += (c['dur'] or 0) / 60
        tot[c['cls']] += 1
        totm[c['cls']] += (c['dur'] or 0) / 60
    out = ['## 2. Tool-call histogram by class (count / tool-wall minutes)\n', 'Fleet totals:\n', '| class | calls | tool-wall min | mean s |', '|---|---|---|---|']
    out += [f'| {k} | {v} | {totm[k]:.1f} | {60 * totm[k] / v:.1f} |' for k, v in tot.most_common()]
    out += ['', 'Per agentType (top classes, count/min):\n']
    for at, rs in sorted(by_at.items(), key=lambda kv: -len(kv[1])):
        out.append(f'- **{at}**: ' + ', '.join(f'{k} {v}/{cls_at_min[at][k]:.0f}m' for k, v in cls_at[at].most_common(14)))
    return out + ['']


def section_tail_table(by_at):
    out = ['## 3. Post-work tail (calls after the last work edit; checkpoint and scratch writes are not "work")\n',
           'Tail elapsed = last work edit result to last non-handback tool result, minus idle gaps and permission waits. Kind: heavy = full-suite test or 2+ test runs or 6+ calls with 4+ verification calls; interim-check = up to 2 calls and at most 1 test; moderate = rest.\n',
           '| agentType | runs w/ work edits | tails non-empty | heavy | moderate | interim | tail elapsed min total | tail tool-min total | tail share of active wall | tail test-runs (full) | median tail calls |',
           '|---|---|---|---|---|---|---|---|---|---|---|']
    g = [0, 0, 0.0, 0.0, 0.0]
    for at, rs in sorted(by_at.items(), key=lambda kv: -len(kv[1])):
        tr = [r for r in rs if r['a']['tail']]
        if not tr:
            continue
        ne = [r for r in tr if r['a']['tail']['n'] > 0]
        kinds = collections.Counter(r['a']['tail']['kind'] for r in tr)
        el = sum(r['a']['tail']['elapsed_min'] for r in tr)
        tm = sum(r['a']['tail']['tool_min'] for r in tr)
        act = sum(r['a']['active'] for r in tr)
        nt = sum(r['a']['tail']['ntest'] for r in tr)
        nf = sum(r['a']['tail']['nfull'] for r in tr)
        out.append(f"| {at} | {len(tr)} | {len(ne)} | {kinds['heavy']} | {kinds['moderate']} | {kinds['interim-check']} | {el:.0f} | {tm:.0f} | {100 * el / act if act else 0:.0f}% | {nt} ({nf}) | {pct([r['a']['tail']['n'] for r in ne], .5) if ne else 0} |")
        g = [g[0] + len(tr), g[1] + len(ne), g[2] + el, g[3] + tm, g[4] + act]
    out.append(f"| **fleet** | {g[0]} | {g[1]} | | | | **{g[2]:.0f}** | **{g[3]:.0f}** | {100 * g[2] / g[4] if g[4] else 0:.0f}% | | |")
    return out + ['']


def section_tail_cost(by_at, runs):
    out = ['### 3b. Tail turn-cost by group (model latency + tool wall per tail call; idle gaps > 10 min and permission waits excluded)\n',
           'Groups: test = test-run; verify = grep/read/lint/git-read/ls/mem; product-run = running what the worker produced; wait = sleep/poll/Monitor; write/git = checkpoint and scratch writes, git.\n',
           '| agentType | runs w/ tail | test min | verify min | product-run min | wait min | write/git min | other min | runs with 5+ verify calls in tail | p90 tail calls |',
           '|---|---|---|---|---|---|---|---|---|---|']
    gsum = collections.Counter()
    for at, rs in sorted(by_at.items(), key=lambda kv: -len(kv[1])):
        tr = [r for r in rs if r['a']['tail'] and r['a']['tail']['n'] > 0]
        if not tr:
            continue
        cs = collections.Counter()
        for r in tr:
            cs.update(r['a']['tail']['cost'])
        gsum.update(cs)
        out.append(f"| {at} | {len(tr)} | {cs['test']:.0f} | {cs['verify']:.0f} | {cs['product-run']:.0f} | {cs['wait']:.0f} | {cs['write/git']:.0f} | {cs['other']:.0f} | {sum(1 for r in tr if r['a']['tail']['nverify'] >= 5)} | {pct([r['a']['tail']['n'] for r in tr], .9)} |")
    out.append(f"| **fleet** | | **{gsum['test']:.0f}** | **{gsum['verify']:.0f}** | {gsum['product-run']:.0f} | {gsum['wait']:.0f} | {gsum['write/git']:.0f} | {gsum['other']:.0f} | | |")
    tcls, tclsm = collections.Counter(), collections.Counter()
    for r in runs:
        t = r['a']['tail']
        if t:
            tcls.update(t['bycls'])
            tclsm.update(t['bycls_min'])
    out += ['', 'Tail calls by class (fleet): ' + ', '.join(f'{k} {v} ({tclsm[k]:.0f} tool-min)' for k, v in tcls.most_common()), '',
            'Same, write-role types only (w-implementer, w-refactorer, w-debugger, w-doc):\n']
    for at in sorted(PRODUCER_TYPES):
        c1, c2 = collections.Counter(), collections.Counter()
        for r in by_at.get(at, []):
            if r['a']['tail']:
                c1.update(r['a']['tail']['bycls'])
                c2.update(r['a']['tail']['bycls_min'])
        out.append(f'- {at}: ' + ', '.join(f'{k} {v} ({c2[k]:.0f}m)' for k, v in c1.most_common(10)))
    return out + ['']


def section_top_tails(runs):
    out = ['### Top 15 runs by tail elapsed\n', '| # | agentType | description | tail min | tail tool-min | calls in tail / total | tail classes | transcript |', '|---|---|---|---|---|---|---|---|']
    tops = sorted([r for r in runs if r['a']['tail']], key=lambda r: -r['a']['tail']['elapsed_min'])[:15]
    for i, r in enumerate(tops, 1):
        t = r['a']['tail']
        cl = ', '.join(f'{k}:{v}' for k, v in sorted(t['bycls'].items(), key=lambda kv: -kv[1]))
        out.append(f"| {i} | {r['a']['at']} | {r['meta'].get('description', '')[:60]} | {t['elapsed_min']:.1f} | {t['tool_min']:.1f} | {t['n']}/{t['ncalls']} | {cl} | `{r['path']}` |")
    return out + ['']


def section_tests(by_at):
    out = ['## 4. Test-run commands by agentType\n', '| agentType | test-run calls | full-suite | targeted | tool-wall min | runs with any test | runs with 3+ tests |', '|---|---|---|---|---|---|---|']
    for at, rs in sorted(by_at.items(), key=lambda kv: -len(kv[1])):
        tc = [(r, c) for r in rs for c in r['calls'] if c['cls'] == 'test-run']
        if not tc:
            continue
        full = sum(1 for _, c in tc if test_scope(c['cmd']) == 'full-suite')
        perrun = collections.Counter(id(r) for r, _ in tc)
        out.append(f"| {at} | {len(tc)} | {full} | {len(tc) - full} | {sum((c['dur'] or 0) for _, c in tc) / 60:.0f} | {len(perrun)} | {sum(1 for v in perrun.values() if v >= 3)} |")
    return out + ['']


def section_permission(allcalls):
    permw = [(r, c) for r, c in allcalls if c.get('perm_user')]
    out = ['## 5. Stalls\n', '### 5-0. Permission-prompt waits (result line carries permissionDecision source user_*)\n',
           f"- {len(permw)} calls, {sum((c['dur'] or 0) for _, c in permw) / 60:.0f} min total (approval wait plus execution), {sum(1 for _, c in permw if (c['dur'] or 0) > 120)} over 120 s; by agentType: "
           + ', '.join(f'{k} {v}' for k, v in collections.Counter(r['a']['at'] for r, _ in permw).most_common())
           + '; by tool: ' + ', '.join(f'{k} {v}' for k, v in collections.Counter(c['name'] for _, c in permw).most_common()),
           '', '| wall s | agentType | class | command (160) | transcript |', '|---|---|---|---|---|']
    for r, c in sorted(permw, key=lambda rc: -(rc[1]['dur'] or 0))[:10]:
        out.append(f"| {c['dur'] or 0:.0f} | {r['a']['at']} | {c['cls']} | `{esc((c['cmd'] or json.dumps(c['input']))[:160])}` | `{r['path']}` |")
    return out + ['', 'All stall tables below EXCLUDE these permission-prompt calls.\n']


def section_long_calls(allcalls):
    long = [(r, c) for r, c in allcalls if (c['dur'] or 0) > 120 and not c.get('perm_user')]
    out = [f'### 5a. Tool calls with wall > 120 s (auto-approved): {len(long)} calls, {sum(c["dur"] for _, c in long) / 60:.0f} min total\n', '| class | calls >120 s | minutes |', '|---|---|---|']
    lc, lcm = collections.Counter(), collections.Counter()
    for r, c in long:
        lc[c['cls']] += 1
        lcm[c['cls']] += c['dur'] / 60
    out += [f'| {k} | {lc[k]} | {v:.0f} |' for k, v in lcm.most_common()]
    out += ['', 'By agentType:\n', '| agentType | calls >120 s | minutes | top classes |', '|---|---|---|---|']
    la = collections.defaultdict(list)
    for r, c in long:
        la[r['a']['at']].append(c)
    for at, cs in sorted(la.items(), key=lambda kv: -sum(c['dur'] for c in kv[1])):
        cc, cm = collections.Counter(), collections.Counter()
        for c in cs:
            cc[c['cls']] += 1
            cm[c['cls']] += c['dur'] / 60
        out.append(f"| {at} | {len(cs)} | {sum(c['dur'] for c in cs) / 60:.0f} | {', '.join(f'{k} {cc[k]}/{v:.0f}m' for k, v in cm.most_common(5))} |")
    unfinished = [c for _, c in allcalls if c.get('res_ts') is None and c['name'] not in END_TOOLS]
    out += ['', f'Tool calls with no tool_result at all (run ended or killed mid-call): {len(unfinished)}; by tool: '
            + ', '.join(f'{k} {v}' for k, v in collections.Counter(c['name'] for c in unfinished).most_common()), '']
    return out


def section_timeouts(allcalls):
    auto_bg = [(r, c) for r, c in allcalls if c.get('timed_out_ms')]
    tt = [(r, c) for r, c in allcalls if c['name'] in SHELL_TOOLS and c.get('timeout_txt') and not c.get('timed_out_ms')]
    big = sum(1 for _, c in allcalls if c['name'] in SHELL_TOOLS and isinstance(c['input'].get('timeout'), (int, float)) and c['input']['timeout'] > 120000)
    return ['### 5b. Timeouts\n',
            f'- Shell calls auto-moved to background after hitting their timeout (`timedOutAfterMs`): {len(auto_bg)}; timeout values: '
            + ', '.join(f'{k} ms x{v}' for k, v in collections.Counter(c['timed_out_ms'] for _, c in auto_bg).most_common())
            + '; classes: ' + ', '.join(f'{k} {v}' for k, v in collections.Counter(c['cls'] for _, c in auto_bg).most_common())
            + '; by agentType: ' + ', '.join(f'{k} {v}' for k, v in collections.Counter(r['a']['at'] for r, _ in auto_bg).most_common()),
            f'- Other shell results mentioning "timed out"/timeout-killed (may include app-level timeouts): {len(tt)}; is_error among them: {sum(1 for _, c in tt if c.get("is_error"))}; classes: '
            + ', '.join(f'{k} {v}' for k, v in collections.Counter(c['cls'] for _, c in tt).most_common(8)),
            f'- Shell calls with an explicit `timeout` input > 120000 ms: {big}', '']


def section_monitor_and_waits(allcalls):
    mons = [(r, c) for r, c in allcalls if c['name'] == 'Monitor']
    out = [f'### 5c. Monitor tool: {len(mons)} calls, {sum((c["dur"] or 0) for _, c in mons) / 60:.0f} tool-min; by agentType: '
           + ', '.join(f'{k} {v}' for k, v in collections.Counter(r['a']['at'] for r, _ in mons).most_common()),
           '', '| agentType | wall s | command (200) | transcript |', '|---|---|---|---|']
    for r, c in sorted(mons, key=lambda rc: -(rc[1]['dur'] or 0))[:15]:
        out.append(f"| {r['a']['at']} | {c['dur'] or 0:.0f} | `{esc((c['input'].get('command') or json.dumps(c['input']))[:200])}` | `{r['path']}` |")
    out += ['', '### 5d. sleep / poll-loop / tail -f / watch / server commands\n']
    for k in ('sleep', 'poll-loop', 'tail-follow', 'watch', 'server'):
        cs = [(r, c) for r, c in allcalls if c['cls'] == k]
        out.append(f"- {k}: {len(cs)} calls, {sum((c['dur'] or 0) for _, c in cs) / 60:.0f} tool-min; agentTypes: " + ', '.join(f'{a} {v}' for a, v in collections.Counter(r['a']['at'] for r, _ in cs).most_common()))
    emb = [c for _, c in allcalls if c['name'] in SHELL_TOOLS and re.search(r'(^|[;&|(\s])(sleep\s+\d|start-sleep)', c['cmd'], re.I)]
    return out + [f"- any shell command containing sleep/Start-Sleep (any class): {len(emb)} calls, {sum((c['dur'] or 0) for c in emb) / 60:.0f} tool-min", '']


def section_background(runs):
    bgs = [(r, b) for r in runs for b in r['a']['bg']]
    notif = [b['notif_after_min'] for _, b in bgs if b['notif_after_min'] is not None]
    return ['### 5e. Background shells (explicit run_in_background or auto-backgrounded on timeout)\n',
            f"- Launches: {len(bgs)} (explicit {sum(1 for _, b in bgs if b['explicit'])}, auto on timeout {sum(1 for _, b in bgs if b['auto'])}) in {len(set(r['aid'] for r, _ in bgs))} runs; by agentType: "
            + ', '.join(f'{k} {v}' for k, v in collections.Counter(r['a']['at'] for r, _ in bgs).most_common()),
            f"- Followed by polling (Monitor / sleep / reads of the output file): {sum(1 for _, b in bgs if b['npolls'])} launches, {sum(b['npolls'] for _, b in bgs)} poll calls, {sum(b['poll_min'] for _, b in bgs):.0f} poll tool-min",
            f"- Completion notification received in-run: {len(notif)}; median minutes launch->notification: {pct(notif, .5):.1f}, p90 {pct(notif, .9):.1f}",
            f"- Never finished inside the transcript (no notification, no TaskStop): {sum(1 for _, b in bgs if not b['finished'])}",
            f"- Idle gaps where the agent ended its turn and was woken by a task-notification: {sum(1 for r in runs for g in r['gaps'] if g[0] == 'notif')} gaps, {sum(r['a']['bg_wait'] for r in runs):.0f} min",
            '', 'Background launch classes: ' + ', '.join(f'{k} {v}' for k, v in collections.Counter(b['cls'] for _, b in bgs).most_common()), '']


def section_top_stalls(runs, allcalls):
    inc = [(c['dur'], r, c['name'] + ':' + c['cls'], (c['cmd'] or json.dumps(c['input']))[:200], c['before'][-300:])
           for r, c in allcalls if c['dur'] and c['dur'] > 120 and c['cls'] != 'web' and not c.get('perm_user')]
    inc += [(g[1], r, 'idle-wait-for-bg-notification', '(agent ended turn; woken by task-notification)', '')
            for r in runs for g in r['gaps'] if g[0] == 'notif' and g[1] > 120]
    inc.sort(key=lambda x: -x[0])
    out = ['### 5f. Top 20 stall incidents (single tool calls by wall time, plus notification-wait gaps)\n',
           '| # | wall s | agentType | kind | command (200) | text before (300) | transcript |', '|---|---|---|---|---|---|---|']
    out += [f"| {i} | {s:.0f} | {r['a']['at']} | {k} | `{esc(cmd)}` | {esc(bef)} | `{r['path']}` |" for i, (s, r, k, cmd, bef) in enumerate(inc[:20], 1)]
    web = [c['dur'] for r, c in allcalls if c['cls'] == 'web' and (c['dur'] or 0) > 120]
    return out + ['', f'Web (WebFetch/WebSearch) calls > 120 s are excluded from the table above: {len(web)} calls, {sum(web) / 60:.0f} min.', '']


def section_handback(runs):
    endbg = [r for r in runs if any(not b['finished'] for b in r['a']['bg'])]
    pw = [r for r in runs if r['parent_waiting_notice']]
    txtw = [r for r in runs if re.search(r'background|still running|waiting for', r['final_text'], re.I) and r['a']['bg']]
    ph = [r for r in runs if r['a']['post_handback']]
    out = ['## 6. Runs ending while waiting on background work\n',
           f"- Runs that kept calling tools AFTER their SubagentHandback: {len(ph)}, {sum(r['a']['post_handback'] for r in ph)} calls",
           f'- Parent saw "waiting on its own background work" for {len(pw)} runs.',
           f'- Runs with a background shell launched but neither notified nor stopped by transcript end: {len(endbg)}',
           f'- Runs with background launches whose final text mentions background/waiting: {len(txtw)}', '',
           '| agentType | description | status | bg launches (unfinished) | parent notice | wall min | transcript |', '|---|---|---|---|---|---|---|']
    for r in sorted({r['aid']: r for r in endbg + pw}.values(), key=lambda r: -r['a']['wall']):
        out.append(f"| {r['a']['at']} | {r['meta'].get('description', '')[:50]} | {r['status']} | {len(r['a']['bg'])} ({sum(1 for b in r['a']['bg'] if not b['finished'])}) | {r['parent_waiting_notice']} | {r['a']['wall']:.0f} | `{r['path']}` |")
    return out + ['']


def section_wait_minutes(runs, allcalls):
    wt = wait_breakdown(runs, allcalls)
    out = ['### 5g. Wait-type minutes (pure waiting, not work)\n', '| wait kind | events | minutes |', '|---|---|---|']
    out += [f'| {k} | {n} | {m:.0f} |' for k, (n, m) in sorted(wt.items(), key=lambda kv: -kv[1][1])]
    out.append(f"| **total** | {sum(v[0] for v in wt.values())} | **{sum(v[1] for v in wt.values()):.0f}** |")
    wa = collections.Counter()
    for r, c in allcalls:
        if not c.get('perm_user') and (c['cls'] in WAIT_CLASSES or c.get('timed_out_ms')):
            wa[r['a']['at']] += (c['dur'] or 0) / 60
    for r in runs:
        wa[r['a']['at']] += r['a']['bg_wait']
    return out + ['', 'Wait minutes by agentType: ' + ', '.join(f'{k} {v:.0f}' for k, v in wa.most_common() if v >= 0.5), '']


def section_cross_role(by_at):
    hp = collections.Counter()
    for r in by_at.get('w-hostile-reviewer', []) + by_at.get('w-hostile-reviewer[wf]', []):
        for c in r['calls']:
            if c['work_edit']:
                fp = (c['input'].get('file_path') or ('shell-edit: ' + c['cmd'][:60])).replace('\\', '/')
                hp[fp.rsplit('/', 1)[0] if '/' in fp else fp] += 1
    out = ['## 7. Cross-role signals\n', '- hostile-reviewer "work edits" by directory: ' + '; '.join(f'`{k}` x{v}' for k, v in hp.most_common(8))]
    for at in ('w-reviewer', 'w-hostile-reviewer', 'w-explorer', 'w-tester'):
        rs = by_at.get(at, [])
        if not rs:
            continue
        tests = [c for r in rs for c in r['calls'] if c['cls'] == 'test-run']
        edits = sum(1 for r in rs for c in r['calls'] if c['work_edit'])
        erun = sum(1 for r in rs if any(c['work_edit'] for c in r['calls']))
        out.append(f"- {at}: {len(rs)} runs, test-run calls {len(tests)} ({sum((c['dur'] or 0) for c in tests) / 60:.0f} tool-min) in {sum(1 for r in rs if any(c['cls'] == 'test-run' for c in r['calls']))} runs; non-checkpoint edits {edits} in {erun} runs")
    rs = by_at.get('w-doc', [])
    b = [sum(1 for c in r['calls'] if c['cls'] == 'build') for r in rs]
    out.append(f"- w-doc: build/compile calls {sum(b)} in {sum(1 for x in b if x)} runs; runs with 5+ builds: {sum(1 for x in b if x >= 5)}; build tool-min {sum((c['dur'] or 0) for r in rs for c in r['calls'] if c['cls'] == 'build') / 60:.0f}")
    status_c = collections.Counter((r['a']['at'], r['status'].split(':')[-1] if r['status'] else 'none') for rs2 in by_at.values() for r in rs2)
    out += ['', 'Status tokens by agentType: ' + '; '.join(f'{at}: ' + ', '.join(f'{s} {v}' for (a, s), v in sorted(status_c.items()) if a == at) for at in sorted(by_at)), '']
    return out


# ---------------------------------------------------------------------------
# Output su disco
# ---------------------------------------------------------------------------

def write_report(path, runs, allcalls, n_files, n_cand, since, until):
    by_at = group_by_type(runs)
    parts = (section_kpi(runs, allcalls, since, until) + section_window(runs, allcalls, n_files, n_cand, since, until)
             + section_types(by_at) + section_histogram(by_at, allcalls) + section_tail_table(by_at) + section_tail_cost(by_at, runs)
             + section_top_tails(runs) + section_tests(by_at) + section_permission(allcalls) + section_long_calls(allcalls)
             + section_timeouts(allcalls) + section_monitor_and_waits(allcalls) + section_background(runs)
             + section_top_stalls(runs, allcalls) + section_handback(runs) + section_wait_minutes(runs, allcalls) + section_cross_role(by_at))
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(parts))


RUN_COLUMNS = ['agent_id', 'agentType', 'description', 'model', 'requestShape', 'effort', 'first_ts', 'wall_min', 'active_min', 'resume_idle_min',
               'bg_wait_min', 'tool_min', 'turns', 'ncalls', 'status', 'tail_kind', 'tail_calls', 'tail_elapsed_min', 'tail_tool_min', 'tail_tests',
               'tail_full_suite', 'tail_classes', 'n_test_runs', 'n_calls_gt120s', 'min_calls_gt120s', 'n_bg', 'n_bg_unfinished', 'n_monitor',
               'parent_waiting_notice', 'path']


def run_row(r):
    a, t = r['a'], r['a']['tail'] or {}
    long = [c['dur'] for c in r['calls'] if (c['dur'] or 0) > 120]
    return [r['aid'], a['at'], r['meta'].get('description', ''), r['models'], r['meta'].get('requestShape', ''), r['meta'].get('effort', ''), r['first_ts'],
            round(a['wall'], 2), round(a['active'], 2), round(a['idle_resume'], 2), round(a['bg_wait'], 2), round(a['tool_min'], 2), r['turns'], len(r['calls']), r['status'],
            t.get('kind', ''), t.get('n', ''), round(t.get('elapsed_min', 0), 2) if t else '', round(t.get('tool_min', 0), 2) if t else '', t.get('ntest', ''), t.get('nfull', ''),
            json.dumps(t.get('bycls', {})) if t else '', sum(1 for c in r['calls'] if c['cls'] == 'test-run'), len(long), round(sum(long) / 60, 2),
            len(a['bg']), sum(1 for b in a['bg'] if not b['finished']), sum(1 for c in r['calls'] if c['name'] == 'Monitor'), r['parent_waiting_notice'], r['path']]


def write_runs_csv(path, runs):
    with open(path, 'w', newline='', encoding='utf-8') as fh:
        w = csv.writer(fh)
        w.writerow(RUN_COLUMNS)
        w.writerows(run_row(r) for r in runs)


def write_calls_csv(path, allcalls):
    with open(path, 'w', newline='', encoding='utf-8') as fh:
        w = csv.writer(fh)
        w.writerow(['agent_id', 'agentType', 'idx', 'tool', 'class', 'test_scope', 'wall_s', 'is_error', 'bg_id', 'timed_out_ms', 'in_tail', 'command', 'path'])
        for r, c in allcalls:
            if c['name'] not in SHELL_TOOLS and (c['dur'] or 0) <= 120:
                continue
            t = r['a']['tail']
            w.writerow([r['aid'], r['a']['at'], c['idx'], c['name'], c['cls'], test_scope(c['cmd']) if c['cls'] == 'test-run' else '',
                        round(c['dur'], 1) if c['dur'] is not None else '', c.get('is_error', ''), c.get('bg_id') or '', c.get('timed_out_ms') or '',
                        bool(t and c['idx'] > t['last_edit_idx']), (c['cmd'] or json.dumps(c['input']))[:500], r['path']])


def parse_args():
    today = datetime.now().strftime('%Y-%m-%d')
    p = argparse.ArgumentParser(description='Audit dei transcript dei subagent (code post-lavoro e attese).')
    p.add_argument('--since', default=(datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d'), help='YYYY-MM-DD, default 30 giorni fa')
    p.add_argument('--until', default=today, help='YYYY-MM-DD incluso, default oggi')
    p.add_argument('--root', default=os.path.expanduser('~/.claude/projects'))
    p.add_argument('--out', required=True, help='cartella di output')
    return p.parse_args()


def main():
    args = parse_args()
    os.makedirs(args.out, exist_ok=True)
    t0 = time.time()
    runs, allcalls, n_files, n_cand = collect(args.root.replace('\\', '/'), args.since, args.until)
    write_report(os.path.join(args.out, 'report.md'), runs, allcalls, n_files, n_cand, args.since, args.until)
    write_runs_csv(os.path.join(args.out, 'runs.csv'), runs)
    write_calls_csv(os.path.join(args.out, 'calls_shell.csv'), allcalls)
    print(f'runs {len(runs)} calls {len(allcalls)} files {n_files} candidates {n_cand} in {time.time() - t0:.0f}s -> {args.out}')


if __name__ == '__main__':
    main()
