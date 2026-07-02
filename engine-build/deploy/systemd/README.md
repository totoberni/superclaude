# JobHunt systemd units (W4 3.8)

Ship-only. These user units are linked but NOT enabled here: enabling the timer
is owner-gated behind a cost + grounding review of the first manual run (spec
sections 5 and 6). No secrets live in the unit files.

## Install (link, do NOT enable)

```bash
systemctl --user link  %h/automations/engine-build/deploy/systemd/jobhunt-daily.service
systemctl --user link  %h/automations/engine-build/deploy/systemd/jobhunt-daily.timer
systemctl --user daemon-reload
```

Replace `%h` with the absolute home path if your shell does not expand it.

## First run (manual, before any timer)

```bash
~/automations/engine-build/bin/jobhunt-daily --no-push --no-draft   # fetch + match smoke
~/automations/engine-build/bin/jobhunt-daily                        # full run with one push
```

Confirm `ANTHROPIC_API_KEY` is unset in the user environment (subscription CLI
only) and capture the runs.jsonl telemetry record.

## Enable (owner-gated, only after review)

```bash
systemctl --user enable --now jobhunt-daily.timer
```
