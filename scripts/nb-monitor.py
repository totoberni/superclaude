#!/usr/bin/env python3
"""
nb-monitor.py — nbclient-based Jupyter notebook runtime monitor (PRODUCER).

Executes a notebook through nbclient's NotebookClient with per-cell hooks and
writes live progress to ~/.claude/.nb-progress.json (schema 1). A companion
statusline READER polls that file at ~1s; this script is the single writer.

What it surfaces (schema 1, see write_progress / PROGRESS_PATH):
  - cell N / total           -> current_index (0-based) + total_cells + current_label
  - per-cell elapsed wall     -> cell_elapsed_s (heartbeat-updated during long cells)
  - tqdm relay                -> last_output_line (last stdout fragment of the live cell)
  - run state classification  -> status + error + kernel

Run-state classification (the load-bearing logic):
  - BROKEN : a cell raised. nbclient surfaces CellExecutionError -> status="broken",
             error = "<ename>: <evalue>" (short traceback summary), kernel="alive".
  - HUNG   : current cell exceeded --cell-timeout (nbclient raises CellTimeoutError,
             a TimeoutError subclass, after interrupting), OR the kernel died
             (DeadKernelError) -> status="hung", kernel="dead".
  - SLOW-but-alive : a cell runs long while still emitting stdout. The monitor does
             NOT mark this hung — it keeps status="running" and the heartbeat keeps
             last_output_line + updated fresh, so the consumer (which decides HUNG by
             staleness of `updated`) sees the run is alive. Only the hard --cell-timeout
             converts a genuinely stuck cell into HUNG.

Usage:
    ~/.claude/.venv/bin/python ~/.claude/scripts/nb-monitor.py run NOTEBOOK \
        [--cell-timeout 300] [--kernel-name NAME] [--progress-path PATH] \
        [--heartbeat 0.5] [--allow-errors]

Exit codes:
    0  notebook ran to completion (status="done")
    1  notebook BROKEN (a cell raised)
    2  notebook HUNG (cell timeout or dead kernel)
    3  usage / load error (bad path, missing dep) — surfaced before any execution
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

DEFAULT_PROGRESS_PATH = Path.home() / ".claude" / ".nb-progress.json"
SCHEMA_VERSION = 1
DEFAULT_CELL_TIMEOUT = 300.0
DEFAULT_HEARTBEAT_S = 0.5
MAX_OUTPUT_LINE = 500      # cap last_output_line length (tqdm bars can be long)
MAX_ERROR_LEN = 400        # cap error summary length


# --------------------------------------------------------------------------- #
# Atomic, fail-safe JSON writer
# --------------------------------------------------------------------------- #
class ProgressWriter:
    """Single-writer atomic state file.

    Every write goes through a temp file in the SAME directory followed by
    os.replace, which is atomic on POSIX. A crash mid-write leaves either the
    previous complete file or the new complete file — never a half-written one.
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, state: dict) -> None:
        state = dict(state)
        state["updated"] = time.time()
        payload = json.dumps(state, ensure_ascii=False, indent=2)
        # Serialise writes from hooks (main thread) and the heartbeat thread.
        with self._lock:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self.path.parent),
                prefix=".nb-progress.",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_name, self.path)
            except BaseException:
                # Best-effort cleanup of the temp file; never leave a turd, and
                # never propagate a writer failure into the execution path.
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise


# --------------------------------------------------------------------------- #
# Monitoring NotebookClient
# --------------------------------------------------------------------------- #
def _build_client_class():
    """Import nbclient lazily so a missing dep is reported as a clean usage
    error (exit 3) rather than an import traceback at module load."""
    from nbclient import NotebookClient

    class MonitoringClient(NotebookClient):
        """NotebookClient that mirrors live execution state into a ProgressWriter.

        - on_cell_start  : records the cell index + start time, resets the live
                           stdout line, writes status="running".
        - output()       : captures the last stdout fragment of the running cell
                           (tqdm relay) as messages arrive.
        - on_cell_executed: writes the post-cell snapshot.
        A background heartbeat thread refreshes cell_elapsed_s / total_elapsed_s /
        last_output_line / updated every `heartbeat_s` seconds while a cell runs,
        so the consumer sees fresh `updated` even during a silent long cell.
        """

        def setup_monitor(self, writer: ProgressWriter, notebook_path: str,
                          total_cells: int, heartbeat_s: float) -> None:
            self._writer = writer
            self._nb_path = notebook_path
            self._total_cells = total_cells
            self._heartbeat_s = heartbeat_s

            self._run_start = time.monotonic()
            self._cell_start = self._run_start
            self._current_index = -1
            self._current_label = ""
            self._last_output_line = ""
            self._status = "idle"
            self._error = None
            self._kernel = "alive"

            self._hb_stop = threading.Event()
            self._hb_thread: threading.Thread | None = None

        # ---- state snapshot ------------------------------------------------ #
        def _snapshot(self) -> dict:
            now = time.monotonic()
            return {
                "schema": SCHEMA_VERSION,
                "notebook": self._nb_path,
                "status": self._status,
                "total_cells": self._total_cells,
                "current_index": self._current_index,
                "current_label": self._current_label,
                "cell_elapsed_s": round(now - self._cell_start, 3),
                "total_elapsed_s": round(now - self._run_start, 3),
                "last_output_line": self._last_output_line,
                "kernel": self._kernel,
                "error": self._error,
            }

        def _flush(self) -> None:
            try:
                self._writer.write(self._snapshot())
            except Exception:
                # A progress-write failure must never abort notebook execution.
                pass

        # ---- heartbeat ----------------------------------------------------- #
        def _heartbeat_loop(self) -> None:
            while not self._hb_stop.wait(self._heartbeat_s):
                if self._status == "running":
                    self._flush()

        def start_heartbeat(self) -> None:
            self._hb_thread = threading.Thread(
                target=self._heartbeat_loop, name="nb-monitor-heartbeat", daemon=True
            )
            self._hb_thread.start()

        def stop_heartbeat(self) -> None:
            self._hb_stop.set()
            if self._hb_thread is not None:
                self._hb_thread.join(timeout=2.0)

        # ---- nbclient hooks ------------------------------------------------ #
        def on_cell_start(self, cell, cell_index):  # noqa: D401
            self._current_index = cell_index
            self._cell_start = time.monotonic()
            self._last_output_line = ""
            self._status = "running"
            tag = _cell_tag(cell)
            self._current_label = tag or f"{cell_index + 1}/{self._total_cells}"
            self._flush()

        def on_cell_executed(self, cell, cell_index, execute_reply):  # noqa: D401
            # Cell finished without raising. Keep status="running" (the run as a
            # whole isn't done yet); just refresh the post-cell snapshot.
            self._flush()

        # ---- stdout capture (tqdm relay) ----------------------------------- #
        def output(self, outs, msg, display_id, cell_index):
            try:
                if msg.get("msg_type") == "stream":
                    text = msg.get("content", {}).get("text", "")
                    line = _last_line(text)
                    if line:
                        self._last_output_line = line[:MAX_OUTPUT_LINE]
            except Exception:
                pass
            return super().output(outs, msg, display_id, cell_index)

    return MonitoringClient


def _cell_tag(cell) -> str:
    """Return the first cell tag as a short label, if any."""
    try:
        tags = cell.get("metadata", {}).get("tags", [])
        if tags:
            return str(tags[0])
    except Exception:
        pass
    return ""


def _last_line(text: str) -> str:
    r"""Last non-empty fragment of a stream chunk.

    tqdm redraws the same line with a leading carriage return, so the live
    progress is the text after the final \r or \n. Returns '' if blank.
    """
    if not text:
        return ""
    # Normalise CR-based redraws then take the last non-empty fragment.
    parts = text.replace("\r", "\n").split("\n")
    for frag in reversed(parts):
        frag = frag.strip()
        if frag:
            return frag
    return ""


def _short_error(exc) -> str:
    """Compact one-line summary of a cell execution error."""
    ename = getattr(exc, "ename", None)
    evalue = getattr(exc, "evalue", None)
    if ename:
        summary = f"{ename}: {evalue}" if evalue else str(ename)
    else:
        summary = f"{type(exc).__name__}: {exc}"
    summary = " ".join(summary.split())  # collapse whitespace/newlines
    return summary[:MAX_ERROR_LEN]


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def run_notebook(notebook_path: Path, progress_path: Path, cell_timeout: float,
                 kernel_name: str | None, heartbeat_s: float,
                 allow_errors: bool) -> int:
    """Execute the notebook with monitoring. Returns a process exit code."""
    import nbformat
    from nbclient.exceptions import CellExecutionError, DeadKernelError

    abs_nb = str(notebook_path.resolve())
    nb = nbformat.read(str(notebook_path), as_version=4)
    total_cells = len(nb.cells)

    writer = ProgressWriter(progress_path)
    MonitoringClient = _build_client_class()

    client_kwargs = dict(
        timeout=cell_timeout,
        # interrupt_on_timeout=False so a stuck cell raises CellTimeoutError
        # directly (-> HUNG). With interrupt=True, nbclient instead injects a
        # KeyboardInterrupt INTO the cell and surfaces it as a CellExecutionError
        # (ename="KeyboardInterrupt"), which would misclassify a hang as BROKEN.
        interrupt_on_timeout=False,
        allow_errors=allow_errors,
        record_timing=False,
    )
    if kernel_name:
        client_kwargs["kernel_name"] = kernel_name

    client = MonitoringClient(nb, **client_kwargs)
    client.setup_monitor(writer, abs_nb, total_cells, heartbeat_s)

    # Initial idle snapshot so the consumer has something immediately.
    client._flush()
    client.start_heartbeat()

    exit_code = 0
    try:
        client.execute()
        client._status = "done"
        client._error = None
    except CellExecutionError as exc:
        if getattr(exc, "ename", None) == "KeyboardInterrupt":
            # Defensive: a timeout-driven interrupt (if interrupt_on_timeout is
            # ever re-enabled) surfaces as a KeyboardInterrupt CellExecutionError.
            # Treat as HUNG, not a genuine user-raised error.
            client._status = "hung"
            client._error = f"cell exceeded {cell_timeout:g}s timeout (interrupted)"
            client._kernel = "dead"
            exit_code = 2
        else:
            # A cell raised -> BROKEN. Kernel is still alive.
            client._status = "broken"
            client._error = _short_error(exc)
            client._kernel = "alive"
            exit_code = 1
    except DeadKernelError as exc:
        # Kernel died -> HUNG, kernel dead.
        client._status = "hung"
        client._error = _short_error(exc)
        client._kernel = "dead"
        exit_code = 2
    except TimeoutError:
        # CellTimeoutError (subclass of TimeoutError): cell exceeded --cell-timeout
        # -> HUNG. The kernel is left unresponsive on the stuck cell. We keep a
        # terse summary rather than nbclient's full cell-source preview.
        client._status = "hung"
        client._error = (f"cell {client._current_index} exceeded "
                         f"{cell_timeout:g}s timeout")
        client._kernel = "dead"
        exit_code = 2
    except Exception as exc:  # noqa: BLE001 — last-resort: surface, don't corrupt state
        client._status = "broken"
        client._error = _short_error(exc)
        exit_code = 1
    finally:
        client.stop_heartbeat()
        client._flush()

    return exit_code


def write_load_error(progress_path: Path, notebook_path: Path, message: str) -> None:
    """Record a pre-execution failure so the consumer sees why nothing ran."""
    try:
        writer = ProgressWriter(progress_path)
        writer.write({
            "schema": SCHEMA_VERSION,
            "notebook": str(notebook_path),
            "status": "broken",
            "total_cells": 0,
            "current_index": -1,
            "current_label": "",
            "cell_elapsed_s": 0.0,
            "total_elapsed_s": 0.0,
            "last_output_line": "",
            "kernel": "dead",
            "error": message[:MAX_ERROR_LEN],
        })
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="nb-monitor.py",
        description="nbclient-based notebook runtime monitor (writes ~/.claude/.nb-progress.json).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="execute a notebook with live progress monitoring")
    run.add_argument("notebook", type=Path, help="path to the notebook to run")
    run.add_argument("--cell-timeout", type=float, default=DEFAULT_CELL_TIMEOUT,
                     help="per-cell timeout in seconds; exceeding it = HUNG (default 300)")
    run.add_argument("--kernel-name", default=None,
                     help="kernelspec name (default: the notebook's own kernelspec)")
    run.add_argument("--progress-path", type=Path, default=DEFAULT_PROGRESS_PATH,
                     help="where to write progress JSON (default ~/.claude/.nb-progress.json)")
    run.add_argument("--heartbeat", type=float, default=DEFAULT_HEARTBEAT_S,
                     help="heartbeat interval in seconds during long cells (default 0.5)")
    run.add_argument("--allow-errors", action="store_true",
                     help="continue past cell errors instead of marking BROKEN")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "run":
        nb_path = args.notebook
        if not nb_path.exists():
            write_load_error(args.progress_path, nb_path, f"notebook not found: {nb_path}")
            print(f"nb-monitor: notebook not found: {nb_path}", file=sys.stderr)
            return 3
        try:
            import nbformat  # noqa: F401
            import nbclient  # noqa: F401
        except ImportError as exc:
            msg = f"missing dependency: {exc.name}"
            write_load_error(args.progress_path, nb_path, msg)
            print(f"nb-monitor: {msg} (install into ~/.claude/.venv)", file=sys.stderr)
            return 3

        return run_notebook(
            notebook_path=nb_path,
            progress_path=args.progress_path,
            cell_timeout=args.cell_timeout,
            kernel_name=args.kernel_name,
            heartbeat_s=args.heartbeat,
            allow_errors=args.allow_errors,
        )

    return 3


if __name__ == "__main__":
    sys.exit(main())
