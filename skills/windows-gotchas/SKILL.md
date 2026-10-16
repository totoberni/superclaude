---
name: windows-gotchas
description: "Use when a failure may stem from native Windows: shells, paths, Python, locks"
category: domain
user-invocable: false
---

# Windows 11 Environment Gotchas

This skill is preloaded by every worker that runs shell commands (`w-implementer`, `w-debugger`, `w-refactorer`, `w-merger`, `w-doc`, `w-reviewer`, `w-hostile-reviewer`, `w-tester`) and is usable by meta. It lists what is TRUE ON THIS MACHINE: native Windows 11, no WSL, no admin rights. Check these before blaming the code.

## Two shell tools

- **Bash tool = Git Bash (MSYS2)**: POSIX syntax, paths as `/c/Users/...`, GNU coreutils present. `jq`, `sqlite3` and `python3` are NOT installed.
- **PowerShell tool**: 7.x and 5.1 are both present. 5.1 has no `&&` / `||` and no ternary operator. `>` and `Out-File` write UTF-16 or a BOM depending on the version, so create files with the Write tool instead.
- Prefer the `mem` / `scpy` shims and the Write tool over shell one-liners: they behave identically in both shells.

## Paths

- Both `C:\...` and `C:/...` work for most tools. `~` resolves to `C:\Users\<user>`.
- **MSYS path conversion**: Git Bash rewrites arguments that look like POSIX paths, so an argument such as `/foo` passed to a native exe may be mangled. Use `MSYS_NO_PATHCONV=1` or write `//foo`.
- NTFS is case-insensitive: `Readme.md` and `README.md` are the same file.
- **The 260-character path limit is ENABLED system-wide here** (long paths are off and cannot be turned on without admin). Git has `core.longpaths=true`, but Python does not benefit. Keep directory nesting shallow.

## Python

- Interpreter: `%LOCALAPPDATA%\Programs\Python\Python312\python.exe`.
- A venv's interpreter is `.venv\Scripts\python.exe`, never `.venv/bin/python`.
- `python3` does not exist. The `python` found first on a stale PATH may be the Microsoft Store stub, which prints an install hint and exits. Use `py -3.12`, `uv`, or an absolute path.
- **Encoding**: the default text encoding is a legacy code page (cp1252) and breaks on Italian accented characters in CSV/text I/O. Set `PYTHONUTF8=1` or pass `encoding="utf-8"` explicitly on every `open()`, `read_text()`, `read_csv()`.

## Line endings

- The repo enforces LF via `.gitattributes`; `core.autocrlf` is false.
- PowerShell-authored files may be CRLF. A CRLF shebang script or SLURM script fails on a Linux host.

## Privileges and isolation

- No symlinks without admin; directory junctions work.
- There is no sandbox on native Windows. Enforcement is the write-ACL guard plus `permissions.deny`.
- No admin rights: install only at user scope (`winget install --scope user`, `uv`, `fnm` for Node).

## Node

- Node comes from `fnm`: `node` / `npx` live in a per-shell `fnm_multishells` directory that changes every session. Never hardcode that path; resolve `node` from PATH.

## File locking

- Windows refuses to delete or rename a file that another process has open. Common with `.db`, `.ipynb`, log files, and anything under OneDrive-synced folders.
- Excel locks any `.xlsx` / `.csv` it has open: a `PermissionError` on write usually means the file is open in Excel.
- Symptom: `PermissionError: [WinError 32]` or "The process cannot access the file because it is being used by another process". Find the holder with `Get-Process`, close it, retry. Do not loop blindly.

## Processes

- No POSIX signals, no `kill -0`, no `nohup`. Use `Get-Process`, `Stop-Process`, and a per-user Task Scheduler task for anything that must outlive the session.

## R and LaTeX

- R is at `%LOCALAPPDATA%\Programs\R\R-4.6.1\bin` (`R`, `Rscript`).
- MiKTeX provides `latexmk` / `pdflatex`. First use of a missing package triggers an on-the-fly install, so the first build of a document can be slow or need network access.

## Quick triage

| Symptom | Likely cause |
|---|---|
| Command prints a Microsoft Store hint and exits | `python` resolved to the Store stub |
| `UnicodeDecodeError` / mangled `à è ì ò ù` | cp1252 default encoding |
| `/flag`-style argument arrives as `C:/Program Files/Git/flag` | MSYS path conversion |
| `FileNotFoundError` on a path that exists | path longer than 260 characters |
| `PermissionError` on delete/rename/write | file held open (Excel, kernel, OneDrive, another process) |
| `&&` parse error | command ran under PowerShell 5.1 |
| File starts with odd bytes or is double-width | written with PowerShell `>` (UTF-16 / BOM) |
