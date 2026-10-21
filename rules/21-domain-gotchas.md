# Domain-Specific Gotchas

Project-specific traps that apply only to certain technology stacks. Loaded as part of the rules tier (after `20-tool-conventions.md`). Cross-referenced from project memory when applicable.

## Test Working-Tree Hygiene

- After any test run that regenerates or rewrites tracked files (generated docs, fixtures, snapshots), restore the files it dirtied. **Be selective**: `git checkout -- docs/` will also revert intentional edits (e.g., model.json). List specific paths instead of broad directories.
- After test runs in another worktree or interpreter, ALWAYS clean `__pycache__` before re-running tests: stale bytecode carries paths from the other environment and causes phantom failures.

## Python Namespace Gotchas

- When a function is imported via `from module_a import func` into `module_b`, patching `module_a.func` does NOT affect `module_b.func` — it has its own reference. You must patch BOTH: `monkeypatch.setattr("module_a.func", ...)` AND `monkeypatch.setattr("module_b.func", ...)`.
- Prefer `monkeypatch` over `@patch` decorators in test fixtures — monkeypatch auto-restores and doesn't leak between tests. `@patch` can leak if the test errors before the decorator's cleanup runs.
- `del sys.modules["X"]; import X` creates a NEW module object. Code that already imported from the old module still holds OLD references. The fix is save/restore in sys.modules, not re-import.

## ISTAT SDMX Quota Is Machine-Wide

- esploradati.istat.it (SDMX) allows 5 queries per minute per IP; going over triggers a silent 1 to 2 day block (TCP timeouts, curl 000 after about 20 s). The quota belongs to the office IP, so every session, project and worker on this laptop shares it.
- Before dispatching ANY worker that touches ISTAT SDMX (any project): grep `~/.claude/comms/_spawns.log` for ISTAT workers of the last 48 h from any session; never run two ISTAT workers at once.
- Inline in every such dispatch: one request every 15 s from a single script with a disk cache, a hard request cap, one probe first, stop at the first timeout; listing dataflows counts as a query. Static files on www.istat.it are a different host.
- A block is never evaded by switching network or IP for an automated fetcher (rate-limit circumvention): wait for the lapse or write to the ISTAT Contact Centre.
- Source: geoscraper 2026-09-22 (own worker over-queried) + 2026-10-05 (probably another session's worker on the shared IP). Occ=2, promoted.

## Windows Detached Processes and Tool Timeouts

- Never pair `DETACHED_PROCESS` with `CREATE_NO_WINDOW` when spawning the venv `.venv\Scripts\python.exe`: it is a launcher stub, and its real-python grandchild gets a new VISIBLE console (Windows Terminal popups on the owner's desktop). Use `CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`, or `pythonw.exe` for a detached supervisor plus `CREATE_NO_WINDOW` on its children (`scripts/job.py`).
- The Bash/PowerShell tool `timeout` parameter does not kill a command: it moves it to the background, where it keeps the worker resident. Wrap slow commands as `timeout <N> <cmd>`; long jobs go through `scpy job.py` or meta's `run_in_background`.
- Source: statusline G-7 (2026-09-30) + job.py WH-2 (2026-10-21, brief written without a Windows-process gotcha search). Occ=2, promoted.

## HDL Register-Lag in NBA Blocks

- In Verilog non-blocking assignment (NBA) `always @(posedge clk)` blocks, `reg_a <= x; reg_b <= reg_a` reads the OLD value of `reg_a` (pre-clock-edge). This is by design but causes bugs when the intent is to use the newly-computed value.
- Fix: add a PREPARE/SETTLE pipeline state between the register write and the dependent read. Never read a register in the same clock cycle it's written via NBA.
- Source: <PROJECT> M-2 (4 occurrences)

## HDL Forward References (Synopsys DC)

- Synopsys DC PRESTO processes Verilog files top-to-bottom. Forward-referenced wires and regs cause elaboration failures that simulators (VCS, Icarus) silently accept.
- Declare ALL wires/regs BEFORE first use in the file. Declare regs/wires BEFORE `generate` blocks.
- Source: <PROJECT> M-12 (3 occ) + M-12a (4 occ)

## Large Image Handling via Read Tool

- Images loaded via the Read tool consume context proportional to their pixel area. A single image wider than ~2000px OR taller than ~2000px can saturate a large fraction of the available context in a single tool call, especially inside a parallel batch that stacks several images.
- **Protocol**: before Read-ing an image from disk, check its dimensions with a short PIL script (`Image.open('<path>').size`; write the script with the Write tool and run it with `python`). If either axis exceeds ~1000px, PIL-crop into tiles each ≤ 1000px and Read each tile in a separate call.
- Crop along natural panel boundaries for multi-panel figures (e.g., a 5-panel horizontal figure → 2-3 tiles at panel edges). Overlap adjacent tiles by ~50-100px so nothing is bisected at the seam and the reader can mentally stitch the view.
- Standard pattern:
  ```python
  import os
  from PIL import Image
  tmp = os.path.expanduser('~/.claude/tmp')
  os.makedirs(tmp, exist_ok=True)
  im = Image.open('<path>')
  W, H = im.size  # confirm dimensions first
  for i, (x0, x1) in enumerate([(0, 1000), (950, 2000), (1950, W)]):
      im.crop((x0, 0, x1, H)).save(f'{tmp}/tile_{i}.png')
  ```
  Then Read each `~/.claude/tmp/tile_N.png` separately, ideally in parallel tool calls.
- This is a context-preservation discipline, not a rendering-fidelity one — the cropped tiles lose no information, they just spread the context cost across multiple Read calls with room for the rest of the conversation in between.
- Source: <PROJECT> Phase-2 hostile-review figure audits (17 figures at 800-1500px each); <PROJECT> session-43 figure-readability review (s1_2d_correlation_c_sweep.png at 2685×543).

See also: `mem search '<project> gotchas'` or `mem list --tier shared-projects` for project-specific gotchas not yet promoted to rule tier.
