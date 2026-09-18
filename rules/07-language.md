# Language Policy

Always-loaded. Applies to ALL agents, every session.

- Reason and communicate in English at all times: chat replies, plans, reports between agents, memory bodies, commit messages. When unsure, default to English.
- Italian is required for: formal deliverables and documents (client reports, formal letters, documents handed to third parties), and comments inside code.
- Never translate identifiers or the code itself: names of variables, functions, classes, files, log keys and CLI flags stay English.

## Examples

Code: English identifiers, Italian comments.

```python
def compute_margin(revenue: float, cost: float) -> float:
    # Il margine è espresso come frazione del ricavo, non in valore assoluto
    return (revenue - cost) / revenue
```

Chat reply: English. "The test fails because the fixture is never reset; fix is in `conftest.py:42`."

Client report (formal deliverable): Italian. "Relazione tecnica: l'analisi dei dati evidenzia un incremento del margine del 4% nel terzo trimestre."
