"""latex-warn: segnala solo i NUOVI warning LaTeX dopo una modifica a un file .tex.

PostToolUse su Write/Edit/MultiEdit, solo per `*.tex`. Non compila mai: legge il
`.log` esistente più vicino (prodotto dalla build dell'utente), estrae firme di
warning stabili, le confronta con l'insieme precedente in cache e riporta solo le
aggiunte, ognuna con un suggerimento in chiaro. Al primo avvistamento di un log
semina la cache in silenzio. Nota morbida: non blocca mai.
"""
from __future__ import annotations

import re
from pathlib import Path

from sc_hooks import core
from sc_hooks.core import Ctx, Result, on

CACHE_DIR = core.DATA_HOME / ".latex-warn-cache"
MAX_LOG_BYTES = 8 * 1024 * 1024

# (regex sul log, sostituzione che toglie i campi volatili). Pagina, numero di riga
# di input e badness cambiano a ogni micro-modifica: tenerli farebbe riapparire come
# "nuovo" un warning invariato.
_LOG_PATTERNS: tuple[tuple[str, tuple[str, str] | None], ...] = (
    (r"LaTeX Warning: (?:Reference|Citation) `[^'\n]+' on page [0-9]+ undefined on input line [0-9]+",
     (r"' on page [0-9]+ undefined on input line [0-9]+", "' undefined")),
    (r"LaTeX Warning: Label `[^'\n]+' multiply defined", None),
    (r"(?:Overfull|Underfull) \\[hv]box \([^)\n]*\) (?:in paragraph |in alignment |detected )?"
     r"at lines [0-9]+--[0-9]+", (r"\([^)]*\)", "(...)")),
    (r"(?:Overfull|Underfull) \\[hv]box \([^)\n]*\) has occurred while \\output is active",
     (r"\([^)]*\)", "(...)")),
    (r"LaTeX Font Warning: Font shape `[^'\n]+' (?:undefined|in size <[^>\n]*> not available)", None),
    (r"(?:! )?LaTeX Error: Too many unprocessed floats", None),
    (r"Package cleveref Error: cleveref must be loaded after hyperref", None),
    (r"Package hyperref Warning: Option `[^'\n]+' has already been used", None),
)
_TEX_BANNER = r"pdfTeX|XeTeX|LuaTeX|This is (pdfeTeX|e?TeX)"
_PKG_LINE = r"^\s*\\(usepackage|RequirePackage)"
_LATE_LOADERS = r"cleveref|bookmark|glossaries|algorithm2e|hypcap|hyperxmp"

# Suggerimenti: il primo predicato vero vince (stesso ordine della versione bash)
_HINTS: tuple[tuple[str, str], ...] = (
    (r"Reference.*undefined", r"undefined \ref - the label is misspelled or not yet defined; rerun the build "
                              "after the label exists"),
    (r"Citation.*undefined", r"undefined \cite - the bibkey is missing from your .bib, or BibTeX/biber has "
                             "not been re-run"),
    (r"multiply defined", r"duplicate \label - two \label{...} share one key; rename one (cross-refs will "
                          "point to the wrong target)"),
    (r"Overfull.*hbox", r"overfull hbox - text runs past the margin; reword, add \- hyphenation, or wrap a "
                        r"long token (e.g. \url, \texttt)"),
    (r"Underfull.*hbox", r"underfull hbox - a line is too loose; usually cosmetic, often from a forced \\ "
                         "or a narrow column"),
    (r"(Overfull|Underfull).*vbox", r"over/underfull vbox - vertical overflow on a page; check large floats "
                                    r"or \vspace near a page break"),
    (r"Font shape", "font substitution - requested shape unavailable; TeX picked a fallback (italic/bold "
                    "may look off)"),
    (r"Too many unprocessed floats", r"FLOAT STACKING - too many figures/tables queued unplaced; add "
                                     r"\clearpage or \FloatBarrier (placeins pkg) to flush them"),
    (r"cleveref must be loaded after hyperref|cleveref loaded before hyperref",
     "HYPERREF ORDER - load cleveref AFTER hyperref (cleveref hooks into hyperref reference machinery)"),
    (r"packages load after hyperref", "HYPERREF ORDER - hyperref should load near-last; packages after it can "
                                      "clobber its redefinitions (cleveref/bookmark are the intended exceptions)"),
    (r"has already been used", r"hyperref option conflict - an option was set twice (often \hypersetup vs "
                               r"\usepackage[...]); consolidate"),
)


def _fs(raw: str, cwd: str = "") -> Path | None:
    """Percorso utilizzabile dal filesystem Windows (accetta anche `/c/Users/...`)."""
    s = str(raw or "").strip().strip('"')
    if not s:
        return None
    m = re.match(r"^/([a-zA-Z])(/|$)", s)
    if m:
        s = f"{m.group(1)}:/{s[m.end():]}"
    p = Path(s).expanduser()
    if not p.is_absolute() and cwd:
        base = _fs(cwd)
        p = base / p if base else p
    return p


def _newest(paths) -> Path | None:
    best, best_t = None, -1.0
    for p in paths:
        try:
            if p.is_file() and p.stat().st_mtime > best_t:
                best, best_t = p, p.stat().st_mtime
        except OSError:
            continue
    return best


def find_log(tex: Path) -> Path | None:
    """Log gemello, poi il `.log` più recente nella stessa cartella, poi entro 2 livelli dal genitore."""
    try:
        sibling = tex.with_suffix(".log")
        if sibling.is_file():
            return sibling
        found = _newest(tex.parent.glob("*.log"))
        if found is None:
            parent = tex.parent.parent
            found = _newest(list(parent.glob("*.log")) + list(parent.glob("*/*.log")))
        return found
    except OSError:
        return None


def _read(path: Path, limit: int = MAX_LOG_BYTES) -> str:
    # latin-1 non fallisce mai: i log TeX possono contenere byte spuri dai percorsi dei font
    with path.open("rb") as fh:
        return fh.read(limit).decode("latin-1")


def log_signatures(text: str) -> set[str]:
    out: set[str] = set()
    for pattern, strip in _LOG_PATTERNS:
        for m in re.finditer(pattern, text):
            sig = m.group(0)
            if strip:
                sig = re.sub(strip[0], strip[1], sig, count=1)
            out.add(" ".join(sig.split()))
    return out


def source_order_signatures(tex_text: str) -> set[str]:
    """Problemi d'ordine hyperref/cleveref letti dal sorgente (il log li mostra solo se esplodono)."""
    lines = tex_text.splitlines()

    def first(pkg: str) -> int:
        rx = re.compile(_PKG_LINE + r"(\[[^]]*\])?\{[^}]*\b" + pkg + r"\b")
        return next((i for i, ln in enumerate(lines) if rx.search(ln)), -1)

    hyp, clv = first("hyperref"), first("cleveref")
    if hyp < 0:
        return set()
    out: set[str] = set()
    if 0 <= clv < hyp:
        out.add("SOURCE hyperref-order: cleveref loaded before hyperref")
    after = sum(1 for ln in lines[hyp + 1:] if re.search(_PKG_LINE, ln) and not re.search(_LATE_LOADERS, ln))
    if after >= 3:
        out.add(f"SOURCE hyperref-order: {after} packages load after hyperref (hyperref should load near-last)")
    return out


def explain(sig: str) -> str:
    for pattern, hint in _HINTS:
        if re.search(pattern, sig):
            return hint
    return "new LaTeX warning"


def _short(sig: str) -> str:
    sig = re.sub(r"^LaTeX (Font )?Warning: ", "", sig)
    sig = re.sub(r"^(! )?LaTeX Error: ", "", sig)
    return re.sub(r"^Package ", "", sig)


@on("PostToolUse", tools=("Write", "Edit", "MultiEdit"), name="latex-warn")
def handle(ctx: Ctx) -> Result | None:
    raw = ctx.file_path
    if not raw.lower().endswith(".tex"):
        return None
    tex = _fs(raw, ctx.cwd)
    try:
        if tex is None or not tex.is_file():
            return None
        log = find_log(tex)
        if log is None:
            return None                            # nessuna build ancora: silenzio
        log_text = _read(log)
        if not re.search(_TEX_BANNER, log_text.split("\n", 1)[0], re.IGNORECASE):
            return None                            # non è un log TeX
        current = log_signatures(log_text) | source_order_signatures(_read(tex, 2 * 1024 * 1024))
    except OSError:
        return None

    import hashlib
    key = hashlib.sha1(core.norm_path(log).encode("utf-8")).hexdigest()
    cache = CACHE_DIR / key
    prior: set[str] | None
    try:
        prior = {ln for ln in cache.read_text(encoding="utf-8").splitlines() if ln}
    except (OSError, UnicodeDecodeError):
        prior = None
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text("".join(s + "\n" for s in sorted(current)), encoding="utf-8", newline="\n")
    except OSError:
        return None                                # senza cache non si può distinguere il nuovo
    if prior is None:
        return None                                # primo avvistamento: si semina la base in silenzio
    new = sorted(current - prior)
    if not new:
        return None

    body = "\n".join(f"  - {_short(s)}\n      hint: {explain(s)}" for s in new)
    return Result.note(
        f"latex-warn: {len(new)} NEW LaTeX warning(s) since the last build of {tex.name} "
        f"(parsed from {log.parent.name}/{log.name}, no recompile triggered):\n{body}\n"
        "These are NEW vs the previous compile state; pre-existing warnings are not repeated. "
        "Soft note only, your tool call was not affected."
    )
