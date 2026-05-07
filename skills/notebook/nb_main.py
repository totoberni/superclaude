"""/notebook skill — CLI dispatcher.

Routes subcommands to module entry points. All subcommands receive the parsed
argparse Namespace; each module owns its own argument schema.

V1.1 — adds: --kernel-name (execute, warm), --no-probe-injection (init),
--warn (audit), --snapshot/--force (batch), nb find, nb revert.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("notebook", type=Path, help="Path to .ipynb (absolute or cwd-relative)")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="nb", description="/notebook skill CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    # init
    s_init = sub.add_parser("init", help="Bootstrap notebook for skill use")
    _add_common(s_init)
    s_init.add_argument("--migrate", action="store_true",
                       help="Re-run install steps idempotently (V1.1)")
    s_init.add_argument("--force", action="store_true",
                       help="Overwrite existing .notebook/ config")
    s_init.add_argument("--no-probe-injection", action="store_true",
                       help="Skip injecting the runtime probe at cell 0")

    # batch
    s_batch = sub.add_parser("batch", help="Atomic multi-cell mutation")
    _add_common(s_batch)
    s_batch.add_argument("--plan", type=Path, required=True, help="YAML plan file")
    s_batch.add_argument("--lock-timeout", type=float, default=30.0)
    s_batch.add_argument("--dry-run", action="store_true")
    s_batch.add_argument("--force-no-example-check", action="store_true")
    s_batch.add_argument("--snapshot", action="store_true",
                        help="Save .notebook/snapshots/<stem>-<ts>.ipynb before mutation")
    s_batch.add_argument("--force", action="store_true",
                        help="Override the git-clean working-tree check")

    # execute
    s_exec = sub.add_parser("execute", help="Run kernel on cells")
    _add_common(s_exec)
    s_exec.add_argument("--cells", default=None, help="Range/IDs/tag selector")
    s_exec.add_argument("--cell-timeout", type=float, default=600.0)
    s_exec.add_argument("--lock-timeout", type=float, default=600.0)
    s_exec.add_argument("--warm-timeout", type=float, default=60.0)
    s_exec.add_argument("--no-warm", action="store_true")
    s_exec.add_argument("--kernel-name", default=None,
                       help="Override the project kernelspec (default: read from "
                            ".notebook/kernel_name written by `nb init`)")
    s_exec.add_argument("--iopub-heartbeat-timeout", type=float, default=30.0,
                       help="V3-X3: seconds of iopub silence before probing kernel "
                            "liveness via ZMQ kernel_info. Default 30s preserves "
                            "V2-C1's fast-fail-on-dead-kernel intent. Bump for long "
                            "Aer / Monte Carlo simulations that emit no intermediate "
                            "iopub messages (e.g. 600.0 for multi-minute cells).")

    # sync
    s_sync = sub.add_parser("sync", help="jupytext --sync wrapper (atomic)")
    _add_common(s_sync)

    # validate
    s_val = sub.add_parser("validate", help="Schema + AST + LaTeX + firewall")
    _add_common(s_val)

    # diff
    s_diff = sub.add_parser("diff", help="Preview plan as nbdime textual diff")
    _add_common(s_diff)
    s_diff.add_argument("--plan", type=Path, required=True)

    # audit
    s_audit = sub.add_parser("audit", help="Read-only audits (em-dash, firewall, ...)")
    _add_common(s_audit)
    s_audit.add_argument("--check", action="append", default=None,
                        help="em-dash | firewall | forbidden-imports | fig-binding")
    s_audit.add_argument("--warn", action="store_true",
                        help="Soft-warn (exit 0) instead of hard-fail (exit 1)")

    # find (V1-H20)
    s_find = sub.add_parser("find", help="List cells whose source/tags/metadata match a regex")
    _add_common(s_find)
    s_find.add_argument("pattern", help="Python regex")
    s_find.add_argument("--in", dest="where", default="source",
                       choices=["source", "tags", "metadata"])

    # revert (V1-H19 + V2-H2 + V2-M3)
    s_revert = sub.add_parser("revert", help="Restore notebook from a snapshot")
    s_revert.add_argument("snapshot", type=Path,
                         help="Path to snapshot .ipynb under .notebook/snapshots/")
    s_revert.add_argument("--target", type=Path, default=None,
                         help="Override the convention-based target resolution")
    s_revert.add_argument("--lock-timeout", type=float, default=30.0)
    s_revert.add_argument("--force-no-example-check", action="store_true")

    # regenerate
    s_regen = sub.add_parser("regenerate", help="Rebuild .ipynb from .py (loses IDs)")
    _add_common(s_regen)
    s_regen.add_argument("--from-py", action="store_true", required=True)

    # reset-kernel (V1-L3: now requires notebook arg)
    s_rk = sub.add_parser("reset-kernel", help="Drop persistent kernel for project")
    s_rk.add_argument("notebook", type=Path,
                     help="Path to .ipynb in the target project (resolves project name)")

    # lock-status
    s_ls = sub.add_parser("lock-status", help="Show notebook lock holder")
    _add_common(s_ls)

    # warm
    s_warm = sub.add_parser("warm", help="Manual cold-start trigger")
    _add_common(s_warm)
    s_warm.add_argument("--warm-timeout", type=float, default=60.0)
    s_warm.add_argument("--kernel-name", default=None)

    # merge-preview (UX-3) — pre-simulate cross-branch merges via git merge-file
    s_mp = sub.add_parser(
        "merge-preview",
        help="Pre-simulate `git merge` of two branches' .ipynb via jupytext+merge-file",
    )
    _add_common(s_mp)
    s_mp.add_argument("ours_branch", help="Branch on which we currently sit (or treat as ours)")
    s_mp.add_argument("theirs_branch", help="Branch we are about to merge in")
    s_mp.add_argument("--base-branch", default=None,
                     help="Common ancestor (default: `git merge-base ours theirs`)")
    s_mp.add_argument("--diff3", action="store_true",
                     help="Use `git merge-file --diff3` to retain base markers")

    args = p.parse_args(argv)

    # V2-M4: translate SkillError subclasses to exit codes at the boundary.
    # All in-skill error paths now raise typed exceptions instead of `sys.exit`,
    # so this dispatcher is the single sys.exit site for user-visible failures.
    try:
        if args.cmd == "init":
            from nb_init import cmd_init
            return cmd_init(args)
        if args.cmd == "batch":
            from nb_edit import cmd_batch
            return cmd_batch(args)
        if args.cmd == "execute":
            from nb_exec import cmd_execute
            return cmd_execute(args)
        if args.cmd == "sync":
            from nb_jupytext import cmd_sync
            return cmd_sync(args)
        if args.cmd == "validate":
            from nb_validate import cmd_validate
            return cmd_validate(args)
        if args.cmd == "diff":
            from nb_edit import cmd_diff
            return cmd_diff(args)
        if args.cmd == "audit":
            from nb_audit import cmd_audit
            return cmd_audit(args)
        if args.cmd == "find":
            from nb_edit import cmd_find
            return cmd_find(args)
        if args.cmd == "revert":
            from nb_edit import cmd_revert
            return cmd_revert(args)
        if args.cmd == "regenerate":
            from nb_jupytext import cmd_regenerate
            return cmd_regenerate(args)
        if args.cmd == "reset-kernel":
            from nb_exec import cmd_reset_kernel
            return cmd_reset_kernel(args)
        if args.cmd == "lock-status":
            from nb_io import cmd_lock_status
            return cmd_lock_status(args)
        if args.cmd == "warm":
            from nb_exec import cmd_warm
            return cmd_warm(args)
        if args.cmd == "merge-preview":
            from nb_merge import cmd_merge_preview
            return cmd_merge_preview(args)
    except KeyboardInterrupt:
        print("[notebook] interrupted", file=sys.stderr)
        return 130
    except Exception as e:  # noqa: BLE001
        # V2-M4: translate SkillError (and its subclasses) to exit 1 with a
        # readable message. Other exceptions re-raise so tracebacks remain
        # visible during development.
        try:
            from nb_edit import SkillError
        except ImportError:
            SkillError = None  # type: ignore[assignment]
        if SkillError is not None and isinstance(e, SkillError):
            print(f"[notebook] {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        raise
    return 2


if __name__ == "__main__":
    sys.exit(main())
