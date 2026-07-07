---
name: wsl-gotchas
description: "WSL2 pitfalls for process, filesystem, networking, Git. For w-debugger."
category: domain
user-invocable: false
---

# WSL Gotchas

Common pitfalls and workarounds when developing on WSL2 Ubuntu 24.04 under Windows 11.

## Process Management

| Gotcha | Explanation | Workaround |
|--------|-------------|------------|
| Background processes survive session close | Node, ngrok, and other daemons started in WSL persist after terminal closes | Always run `lsof -i :PORT` and `pgrep -f process_name` before starting services |
| Port conflicts between WSL and Windows | A Windows process may hold a port that WSL also tries to bind | Check both sides: `ss -tlnp` in WSL, `netstat -ano` in PowerShell |
| curl double output | `curl -w '%{http_code}' URL \|\| echo "000"` prints body + code + "000" on failure | Use assignment form: `CODE=$(curl -s -o /dev/null -w '%{http_code}' URL)` |

## File System

| Gotcha | Explanation | Workaround |
|--------|-------------|------------|
| /mnt/c/ is slow | Windows filesystem mounted via 9P is orders of magnitude slower for I/O-heavy operations | Keep all project files on native Linux filesystem (~/projects/) |
| inotify unreliable across boundary | File watchers (webpack, nodemon) may not detect changes on /mnt/c/ | Use polling mode or keep source on Linux side |
| Line endings (CRLF vs LF) | Windows tools may introduce CRLF; Git may auto-convert | Set `.gitattributes` with `* text=auto eol=lf` and `git config core.autocrlf input` |
| Zone.Identifier files | Windows creates `:Zone.Identifier` alternate data stream files when downloading | Filter in listings: `ls \| grep -v Zone.Identifier`; add to .gitignore |

## Networking

| Gotcha | Explanation | Workaround |
|--------|-------------|------------|
| localhost bidirectional | WSL2 and Windows share localhost — services on either side are accessible | Use `localhost` not `127.0.0.1` for consistency |
| ngrok tunnel binding | ngrok needs explicit port on localhost | Use `ngrok http localhost:PORT` not `ngrok http 0.0.0.0:PORT` |

## Git & SSH

| Gotcha | Explanation | Workaround |
|--------|-------------|------------|
| SSH agent not auto-started | SSH keys aren't available until agent is running | Add `eval "$(ssh-agent -s)"` and `ssh-add` to .bashrc |
| Git credential manager | WSL Git doesn't have its own credential store by default | Use Windows GCM: `git config --global credential.helper "/mnt/c/Program Files/Git/mingw64/bin/git-credential-manager.exe"` |
