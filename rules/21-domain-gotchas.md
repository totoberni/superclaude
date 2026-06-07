# Domain-Specific Gotchas

Project-specific traps that apply only to certain technology stacks. Loaded as part of the rules tier (after `20-tool-conventions.md`). Cross-referenced from project memory when applicable.

## Compose / Docker Test Hygiene

- After any test run that uses compose volumes, restore host files dirtied by bind mounts. **Be selective** — `git checkout -- docs/` will also revert intentional edits (e.g., model.json). List specific paths instead of broad directories.
- `sg docker -c "docker ..."` is needed in Claude Code bash tool sessions. The bash tool spawns new shells without the docker group — `sg docker -c` re-enters the group.
- After compose test runs, ALWAYS clean `__pycache__` before running host tests — compose may produce bytecode with container-specific paths.

## WSL File Permissions

- WSL/Windows strips the Unix executable bit (`755 → 644`) on NTFS-mounted files, producing phantom `mode change` diffs with zero content changes.
- All <PROJECT> project repos on WSL should have `core.fileMode=false` set (repo-local, not global) to ignore permission-only diffs.
- **Never commit mode-only diffs** — scripts need `+x` for Docker/VPS deployment. If you see `0 insertions, 0 deletions` staged changes, check `git diff --cached --summary` for mode changes and discard them with `git restore --staged . && git checkout -- .`

## Python Namespace Gotchas

- When a function is imported via `from module_a import func` into `module_b`, patching `module_a.func` does NOT affect `module_b.func` — it has its own reference. You must patch BOTH: `monkeypatch.setattr("module_a.func", ...)` AND `monkeypatch.setattr("module_b.func", ...)`.
- Prefer `monkeypatch` over `@patch` decorators in test fixtures — monkeypatch auto-restores and doesn't leak between tests. `@patch` can leak if the test errors before the decorator's cleanup runs.
- `del sys.modules["X"]; import X` creates a NEW module object. Code that already imported from the old module still holds OLD references. The fix is save/restore in sys.modules, not re-import.

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
- **Protocol**: before Read-ing an image from disk, check its dimensions with `python3 -c "from PIL import Image; im = Image.open('<path>'); print(im.size)"`. If either axis exceeds ~1000px, PIL-crop into tiles each ≤ 1000px and Read each tile in a separate call.
- Crop along natural panel boundaries for multi-panel figures (e.g., a 5-panel horizontal figure → 2-3 tiles at panel edges). Overlap adjacent tiles by ~50-100px so nothing is bisected at the seam and the reader can mentally stitch the view.
- Standard pattern:
  ```python
  from PIL import Image
  im = Image.open('<path>')
  W, H = im.size  # confirm dimensions first
  for i, (x0, x1) in enumerate([(0, 1000), (950, 2000), (1950, W)]):
      im.crop((x0, 0, x1, H)).save(f'/tmp/tile_{i}.png')
  ```
  Then Read each `/tmp/tile_N.png` separately, ideally in parallel tool calls.
- This is a context-preservation discipline, not a rendering-fidelity one — the cropped tiles lose no information, they just spread the context cost across multiple Read calls with room for the rest of the conversation in between.
- Source: <PROJECT> Phase-2 hostile-review figure audits (17 figures at 800-1500px each); <PROJECT> session-43 figure-readability review (s1_2d_correlation_c_sweep.png at 2685×543).

See also: `memory_db.py search '<project> gotchas'` or `list --tier shared-projects` for project-specific gotchas not yet promoted to rule tier.
