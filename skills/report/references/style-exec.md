# Executive style

> Part of /report (see ../SKILL.md). Default mode.

The reader is a manager or a client who reads the first page and skims the rest.
The report must let them repeat the result to someone else in two sentences, and
trust it because the scale of the work and the checks are visible.

Italian prose patterns (calques, fillers, hedging) are owned by
`~/.claude/skills/deliverable-it/references/patterns-it.md`: run the draft
against it. This file covers what is specific to the executive PDF.

## Rules

1. **One idea per paragraph, short sentences.** Aim for 15 to 25 words per sentence. Paragraphs of 2 to 4 sentences.
2. **First person plural, active voice.** "Abbiamo incrociato", "abbiamo verificato". The reader should see who did the work.
3. **Explain every technical idea in everyday words.** An index is followed, in the same paragraph, by what it measures, its scale and what a good value is.
4. **Codes only in tables.** Rule codes, variable names, column names and file paths never appear in prose. File names of delivered materials are allowed only in "Materiali consegnati".
5. **State the scale, do not inflate it.** Volumes, sources crossed, cases checked one by one, alternatives evaluated and rejected. A claim of effort is always a number.
6. **Numbers carry their unit and base.** "il 19,4% delle imprese del perimetro", never "il 19,4%" alone. Every number is in the numbers checklist.
7. **Limits have a size.** Each limit says how many units it touches and how to read around it.
8. **Honest method sentence.** If AI-assisted tools were used, one plain sentence says for what, and where each judgement and its source are recorded.
9. **Headings are statements of content, sentence case.** "I dati che abbiamo usato", not "Dati e Fonti".

## Examples

### Explaining an index

Bad (jargon, codes, no meaning for the reader):

> Cohen's quadratic weighted kappa tra `score_regole` e `score_analista` è 0,698 (n=1626), exact match 58,5%, within-1 96,6%.

Good (from the ExcITA report):

> L'accordo misurato è un kappa pesato di 0,698, con il 58,5% di score identici e il 96,6% entro un punto. Il kappa dice quanto due valutazioni concordano al netto dell'accordo dovuto al caso (0 vuol dire a caso, 1 accordo perfetto), e la versione pesata conta di più un 1 contro un 5 che un 2 contro un 3: un valore vicino a 0,7 indica un accordo sostanziale.

### Rules in prose

Bad (rule codes and flags in the text):

> Le imprese in N3m con `gruppo_estero = True` e ATECO manifatturiero sono state riassegnate da M3 a S4.

Good (the lesson, in words; the codes stay in the rules table):

> Uno stabilimento italiano controllato dall'estero è una vera fabbrica e non una filiale commerciale, quindi resta neutro (3) e non scende a 4.

### Stating the effort

Bad (understated and vague):

> Sono state effettuate alcune verifiche sui casi più dubbi.

Good (scale and outcome, factual):

> Ogni caso è stato rivisto con fonti pubbliche e chiuso con uno score e una motivazione. In tutto le imprese verificate sono 989, tutte nel perimetro: per 658 la verifica ha confermato lo score delle regole, per 331 lo ha cambiato.

### AI-assisted work

Bad (hides the tool, or overclaims it):

> L'intelligenza artificiale ha classificato automaticamente tutte le imprese.

Good:

> Le verifiche mirate sono state svolte con strumenti di ricerca assistiti dall'intelligenza artificiale. Ogni giudizio è registrato nel file con la sua fonte, così che chiunque possa ripercorrerlo.

## The "In sintesi" box

- 3 to 5 bullets, each starting with a bold label and carrying one headline number.
- Cover: the scale (how many units, all scored or covered), the main result, the checks done and their outcome, one agreement or quality figure if there is one.
- No new information that is not developed later in the report.

## Sources table

Columns `Fonte | Che cosa contiene | Perché la usiamo | Copertura e note`. The last
column carries licence, update date and how many units were matched. After the
table, or before it, one paragraph lists the alternatives considered and
rejected, each with its reason (not downloadable, cannot be matched, too costly
for the timeline). This is where the breadth of the search shows.
