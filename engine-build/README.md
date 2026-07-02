# Automations engine (W3 core, fixtures-only v1)

Shared engine for the three daily summative automations (jobhunt / phd / papers).
One engine, three thin instances that supply only config. v1 is fixtures-only:
no live discovery, no submission, no network calls.

## Setup

```bash
~/.claude/.venv/bin/python -m pytest tests/ -q   # run from this directory
```

Requires Python 3.11+ and the stdlib plus `pyyaml`. No other dependencies
(sqlite3 is stdlib; FTS5 is used for domain-memory search when the build has it,
with a LIKE fallback otherwise). Import path is set by `pytest.ini` (`pythonpath = .`).

## Usage

A daily pass wires the modules together:

```python
from engine import load_config, SSOT, Store, run_discovery
from engine.discover import GreenhouseAdapter, LeverAdapter, AshbyAdapter
from engine.match import Scorer, profile_from_ssot
from engine.notify import NtfyTransport, load_credentials, publish_digest
from engine.queue_sm import QueueStateMachine

config = load_config("instances/jobhunt/config.yaml")
ssot = SSOT.load("job.yaml")                 # read-only; MISSING is never guessed
store = Store("jobhunt/store.db")            # one db per automation, toto-only
scorer = Scorer(config, profile_from_ssot(ssot))
queue = QueueStateMachine(store, config)

sources = [(GreenhouseAdapter(), gh_json, "acme"),
           (LeverAdapter(), lever_json, "globex"),
           (AshbyAdapter(), ashby_json, "initech")]
for posting in run_discovery(sources, store):   # liveness + ledger dedup applied
    queue.enqueue(posting, scorer.score(posting))
rerank = queue.rerank()                          # daily re-rank + buffer demotion

transport = NtfyTransport(load_credentials("~/automations/ntfy/credentials"))
publish_digest(transport, config.topic, queue.items(), len(rerank.demoted_today))
```

Missing required fields park the item and drive one questionnaire item; the reply
updates the SSOT and resumes the parked item (`engine.questionnaire`). Tests
inject `FakeTransport` instead of `NtfyTransport`, so no network is touched.

## Configuration

Each instance is one `config.yaml` under `instances/<name>/`:

| Key | Meaning |
|-----|---------|
| `topic` | ntfy topic (`abe-jobsearch` / `abe-phd` / `abe-papers`) |
| `id_prefix` | short-id prefix (`j-` / `phd-` / `p-`), monotonic + never re-used |
| `threshold` | surface score at or above this (jobs 70, phd 70, papers 50) |
| `buffer_size` | standing buffer = visible cap (50 / 15 / 20) |
| `terminal_state` | `submitted` (jobhunt) or `pending_review` (phd, papers) |
| `ssot` | identikit file name (`job.yaml` / `academic.yaml`) |
| `scoring.axes` | weighted axis map; weights must sum to 1.0 |
| `ats_rules` | show-and-warn hard-filter patterns + required capability |
| `channels.automatable` | vendors whose apply form is public + login-free |

The jobs axis functions are implemented in `engine/match.py`. The phd/papers axis
sets in their configs are design placeholders (per plan 7.3, to be designed in a
later wave); building a `Scorer` from them fails fast until they land.

ntfy credentials live at `~/automations/ntfy/credentials` (0600, key=value lines:
`url` / `user` / `password` / `token`); loading is fail-closed if the file is
absent or world-readable.
