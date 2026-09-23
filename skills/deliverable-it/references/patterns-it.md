# Italian prose patterns: detect and rewrite

Working reference for `/deliverable-it` step 4. Loaded on demand, never always
loaded. The frame is English (agent facing, rule 07); every example is Italian,
because the deliverable is.

Structural inspiration: <https://github.com/conorbronsdon/avoid-ai-writing>
(MIT), which catalogues English patterns in a detect plus rewrite shape. Nothing
is copied from it; the English catalogue does not transfer, so the patterns
below are written for Italian consulting prose.

How to use: pass the draft against every entry. Each hit is either rewritten in
place or recorded as an accepted false positive with a reason. Report both
counts.

Note on dashes: rule 06 forbids the em dash and the en dash in anything we
author, ranges included. Every "Dopo" below respects it.

Note on accents: write the real accented vowel (è, à, ù, ò, ì, é). Spelling it
as a bare vowel followed by an apostrophe is a legacy of ASCII keyboards and
reads as an unfinished draft in a client document. Apostrophes are for elisions
only (l'analisi, un'area, dell'immobile).

---

## A. Calques and false friends from English

### 1. `implementare` for `attuare`
- **Pattern**: `implementare` applied to a policy, a process or a measure.
- **Why it reads machine made**: direct transfer of "to implement"; in Italian
  consulting prose it belongs to software, not to decisions.
- **Rewrite**: `attuare`, `realizzare`, `avviare`, `introdurre`.
- **Prima**: Il comune ha implementato un piano di rigenerazione urbana.
- **Dopo**: Il comune ha avviato un piano di rigenerazione urbana.

### 2. `supportare`
- **Pattern**: `supportare` used for "to support" in every sense.
- **Why**: one English verb flattened onto four Italian ones.
- **Rewrite**: `sostenere` (economically), `confermare` (evidence), `consentire`
  (technically), `assistere` (a person).
- **Prima**: I dati supportano questa conclusione.
- **Dopo**: I dati confermano questa conclusione.

### 3. `performante`, `performance`
- **Pattern**: `performante`, `le performance del comparto`.
- **Why**: jargon borrowed wholesale; says nothing measurable.
- **Rewrite**: name the measured quantity: `redditività`, `rendimento`,
  `risultati`, `efficienza`.
- **Prima**: Il segmento uffici ha mostrato performance migliori.
- **Dopo**: Il segmento uffici ha reso l'1,8% in più del residenziale.

### 4. `in termini di`
- **Pattern**: `in termini di X` as a universal connector.
- **Why**: fills the slot where a precise preposition or verb belongs.
- **Rewrite**: use the noun directly, or `quanto a`, `per`, `dal punto di vista`.
- **Prima**: Il divario è rilevante in termini di occupazione.
- **Dopo**: Il divario occupazionale è rilevante.

### 5. `basato su`
- **Pattern**: `basato su` for "based on", especially opening a sentence.
- **Why**: participial English scaffolding; Italian prefers a finite verb.
- **Rewrite**: `a partire da`, `secondo`, `sulla base di` (sparingly), or recast.
- **Prima**: Basato sui dati ISTAT, il mercato risulta in crescita.
- **Dopo**: I dati ISTAT mostrano un mercato in crescita.

### 6. `indirizzare` a problem
- **Pattern**: `indirizzare una criticità` for "to address an issue".
- **Why**: `indirizzare` in Italian means to route or to aim, not to tackle.
- **Rewrite**: `affrontare`, `risolvere`, `intervenire su`.
- **Prima**: Il piano indirizza il tema della morosità.
- **Dopo**: Il piano affronta il tema della morosità.

### 7. Untranslated business nouns
- **Pattern**: `asset`, `driver`, `key player`, `benchmark`, `target` in running
  Italian prose.
- **Why**: signals a draft assembled from English sources.
- **Rewrite**: `patrimonio` or `immobile`, `fattore` or `determinante`,
  `operatore principale`, `riferimento`, `obiettivo`. Keep the English term only
  where it is the client's own vocabulary.
- **Prima**: I principali driver del mercato restano i tassi.
- **Dopo**: Il mercato resta determinato soprattutto dai tassi.

### 8. Verbs coined from English
- **Pattern**: `schedulare`, `processare`, `deliverare`, `skillato`, `matchare`.
- **Why**: gergo interno leaking into a client document.
- **Rewrite**: `programmare`, `elaborare`, `consegnare`, `qualificato`,
  `abbinare`.
- **Prima**: Le estrazioni vengono schedulate ogni notte.
- **Dopo**: Le estrazioni sono programmate ogni notte.

### 9. `realizzare` meaning "to notice"
- **Pattern**: `si è realizzato che`.
- **Why**: false friend of "to realize".
- **Rewrite**: `ci si è resi conto che`, `è emerso che`, `risulta che`.
- **Prima**: Si è realizzato che il campione era distorto.
- **Dopo**: È emerso che il campione era distorto.

### 10. `eventualmente` meaning "eventually"
- **Pattern**: `eventualmente` used for a future certainty.
- **Why**: false friend; in Italian it means "possibly", not "in the end".
- **Rewrite**: `alla fine`, `col tempo`, `entro il 2027`.
- **Prima**: I canoni eventualmente si allineeranno all'inflazione.
- **Dopo**: I canoni si allineeranno all'inflazione entro due anni.

### 11. `consistente` meaning "consistent"
- **Pattern**: `dati consistenti con l'ipotesi`.
- **Why**: false friend; `consistente` means substantial, not coherent.
- **Rewrite**: `coerente con`, `compatibile con`.
- **Prima**: Il risultato è consistente con la letteratura.
- **Dopo**: Il risultato è coerente con la letteratura.

### 12. `significativo` without statistics
- **Pattern**: `un aumento significativo` where no test was run.
- **Why**: borrows statistical authority the analysis does not have.
- **Rewrite**: `rilevante`, `marcato`, or state the magnitude. Keep
  `significativo` only next to a test and its level.
- **Prima**: Si osserva un aumento significativo delle compravendite.
- **Dopo**: Le compravendite crescono del 6,4% sul 2024.

### 13. `attuale` and `puntuale` misuse
- **Pattern**: `attuale` for "actual", `puntuale` for "punctual" only.
- **Why**: false friends that flip the meaning.
- **Rewrite**: `effettivo` or `reale` for actual; `puntuale` is fine for
  "point by point", not for on time (`in orario`).
- **Prima**: Il valore attuale differisce dalla stima.
- **Dopo**: Il valore effettivo differisce dalla stima.

### 14. Gerund chains borrowed from English
- **Pattern**: `analizzando i dati e confrontando i segmenti, emerge che`.
- **Why**: English participial openers stacked; Italian reads it as suspended.
- **Rewrite**: split into finite clauses.
- **Prima**: Analizzando i dati e confrontando i segmenti, emerge una divergenza.
- **Dopo**: Il confronto fra i segmenti mostra una divergenza.

---

## B. Openers, fillers and closings

### 15. `In un mondo sempre più`
- **Pattern**: the cosmic opener: `in un mondo sempre più complesso`,
  `in uno scenario sempre più competitivo`.
- **Why**: the single strongest machine made signal in Italian; it carries no
  information and could open any report on any subject.
- **Rewrite**: delete it and start from the fact.
- **Prima**: In un contesto sempre più competitivo, la logistica assume un ruolo centrale.
- **Dopo**: Fra il 2021 e il 2025 la logistica ha assorbito il 38% degli investimenti immobiliari.

### 16. `È importante sottolineare che`
- **Pattern**: `è importante sottolineare che`, `va evidenziato come`,
  `giova ricordare che`.
- **Why**: the sentence announces its own importance instead of being important.
- **Rewrite**: delete the opener, keep the clause.
- **Prima**: È importante sottolineare che il campione esclude i comuni sotto i 5.000 abitanti.
- **Dopo**: Il campione esclude i comuni sotto i 5.000 abitanti.

### 17. `Non è un caso che`
- **Pattern**: `non è un caso che`, `non sorprende che`.
- **Why**: asserts causality without establishing it.
- **Rewrite**: state the mechanism, or drop the claim.
- **Prima**: Non è un caso che i prezzi salgano dove l'offerta è rigida.
- **Dopo**: Dove l'offerta è rigida i prezzi salgono più in fretta: l'elasticità stimata è 0,3.

### 18. `Negli ultimi anni si è assistito a`
- **Pattern**: the vague historical opener with an impersonal witness.
- **Why**: no agent, no dates, no source.
- **Rewrite**: put the years and the subject in.
- **Prima**: Negli ultimi anni si è assistito a una crescita della domanda.
- **Dopo**: Dal 2022 al 2025 la domanda è cresciuta del 12%.

### 19. `Occorre premettere che`
- **Pattern**: a preamble announcing a preamble.
- **Why**: delays the content by one paragraph.
- **Rewrite**: if it is a caveat, it belongs in `Metodo e fonti`; otherwise cut.
- **Prima**: Occorre premettere che i dati sono provvisori.
- **Dopo**: I dati del 2025 sono provvisori (aggiornamento previsto a marzo).

### 20. `Alla luce di quanto esposto`
- **Pattern**: `alla luce di quanto esposto`, `sulla base di quanto precede`.
- **Why**: bureaucratic hinge; the reader already read what precedes.
- **Rewrite**: `quindi`, `di conseguenza`, or nothing.
- **Prima**: Alla luce di quanto esposto, si raccomanda di rinviare l'investimento.
- **Dopo**: Conviene quindi rinviare l'investimento di dodici mesi.

### 21. `In conclusione possiamo affermare che`
- **Pattern**: the closing that restates the opening.
- **Why**: adds a paragraph with zero new content.
- **Rewrite**: end on the last real finding, or on the decision it implies.
- **Prima**: In conclusione, possiamo affermare che il mercato resta solido.
- **Dopo**: Il mercato resta solido finché i tassi non superano il 4%.

---

## C. Abstraction, hedging, structure

### 22. Stacked abstract nouns
- **Pattern**: three or more abstract nouns chained by `di`:
  `l'ottimizzazione della valorizzazione del patrimonio`.
- **Why**: each `di` hides the actor; nothing can be verified or acted on.
- **Rewrite**: at most one `di` chain; turn one noun back into a verb.
- **Prima**: La razionalizzazione della gestione della manutenzione degli immobili.
- **Dopo**: Gestire la manutenzione con un solo fornitore.

### 23. Nominalisation instead of a verb
- **Pattern**: `procedere alla verifica`, `effettuare una valutazione`,
  `dare attuazione`.
- **Why**: a light verb plus a noun where one verb suffices; doubles the words.
- **Rewrite**: use the verb.
- **Prima**: Si procederà alla verifica dei dati catastali.
- **Dopo**: Verificheremo i dati catastali.

### 24. Hedging chains
- **Pattern**: two or more hedges in one clause: `potrebbe tendenzialmente
  risultare in parte`, `sembrerebbe possibile ipotizzare`.
- **Why**: each hedge is defensible; stacked they read as evasion.
- **Rewrite**: one hedge, then quantify the uncertainty.
- **Prima**: Il dato potrebbe tendenzialmente indicare una possibile ripresa.
- **Dopo**: Il dato indica una ripresa, con un intervallo di confidenza ampio (più o meno 2,1 punti).

### 25. Empty triads
- **Pattern**: three near synonyms in a row: `efficace, efficiente e sostenibile`.
- **Why**: rhythm substituted for meaning; the third term is usually free.
- **Rewrite**: keep the one term that is measured.
- **Prima**: Un intervento efficace, efficiente e sostenibile.
- **Dopo**: Un intervento che rientra nel budget di 4,2 mln.

### 26. Bullet list where a paragraph is due
- **Pattern**: three items that share a subject and a causal link, broken into
  bullets.
- **Why**: bullets remove the connective tissue; the reader has to rebuild the
  argument. A reasoning chain is prose.
- **Rewrite**: bullets for genuinely parallel, independent items (sources,
  scenarios, deliverables). Prose for anything with `perché`, `quindi`, `ma`.
- **Prima**: Elenco: domanda in crescita; offerta rigida; prezzi in aumento.
- **Dopo**: La domanda cresce mentre l'offerta resta rigida, e i prezzi salgono di conseguenza.

### 27. Bullets that restate the lead-in
- **Pattern**: `I fattori sono tre:` followed by bullets each starting
  `Il fattore ...`.
- **Why**: the stem is repeated in every branch.
- **Rewrite**: make the bullets complete the stem grammatically.
- **Prima**: I rischi sono: il rischio tassi; il rischio normativo.
- **Dopo**: I rischi principali sono i tassi e la normativa regionale.

### 28. Symmetric paragraph openings
- **Pattern**: every paragraph of a section opens the same way
  (`Inoltre`, `Inoltre`, `Infine`).
- **Why**: generated text tends to a fixed cadence; a human varies.
- **Rewrite**: open with the subject of the paragraph, not with a connector.
- **Prima**: Inoltre, il comparto logistico cresce.
- **Dopo**: Il comparto logistico cresce anche nel 2025.

### 29. The `non solo ... ma anche` scaffold
- **Pattern**: used two or more times in the same document.
- **Why**: a template that manufactures emphasis without adding a fact.
- **Rewrite**: coordinate plainly, or split into two sentences.
- **Prima**: Non solo i canoni crescono, ma anche i rendimenti migliorano.
- **Dopo**: Crescono i canoni e migliorano i rendimenti.

### 30. Rhetorical question as a transition
- **Pattern**: `Che cosa significa tutto questo?` opening a section.
- **Why**: a talk device; in a written report it postpones the answer.
- **Rewrite**: give the answer as the heading or the first sentence.
- **Prima**: Quali sono le implicazioni? Le implicazioni sono tre.
- **Dopo**: Le implicazioni sono tre.

### 31. Summary paragraph with no new content
- **Pattern**: the last paragraph of each section recaps the section.
- **Why**: padding; the reader just read it.
- **Rewrite**: delete, unless the recap carries a number the section derived.
- **Prima**: In sintesi, quanto sopra descrive un mercato in evoluzione.
- **Dopo**: (cancellato)

### 32. `Da un lato ... dall'altro` without contrast
- **Pattern**: the frame used for two facts that do not oppose.
- **Why**: signals a contrast the content does not deliver.
- **Rewrite**: coordinate, or find the real tension.
- **Prima**: Da un lato i prezzi salgono, dall'altro cresce la domanda.
- **Dopo**: I prezzi salgono trainati dalla domanda.

---

## D. Headings and typography

### 33. English title case in Italian headings
- **Pattern**: `Analisi Del Mercato Del Lavoro In Piemonte`.
- **Why**: Italian uses sentence case; capitalised function words are a direct
  English import and the most visible surface tell.
- **Rewrite**: capital on the first word and on proper nouns only.
- **Prima**: Analisi Del Mercato Del Lavoro In Piemonte
- **Dopo**: Analisi del mercato del lavoro in Piemonte

### 34. Headings written as full sentences
- **Pattern**: `Il mercato degli uffici ha rallentato nel corso del 2025.`
- **Why**: a heading is a label; a final period marks it as prose.
- **Rewrite**: nominal phrase, no final period. Keep the claim for the first
  sentence of the section.
- **Prima**: Il mercato degli uffici ha rallentato nel 2025.
- **Dopo**: Uffici: rallentamento nel 2025

### 35. Bold inside running prose
- **Pattern**: three or more bold fragments per page for emphasis.
- **Why**: emphasis distributed everywhere is emphasis nowhere; reads as slide
  text pasted into a report.
- **Rewrite**: bold only for defined terms at first use and for table labels.
  For emphasis, put the word at the end of the sentence.
- **Prima**: Il **canone medio** cresce del **4,2%**, trainato dalla **domanda logistica**.
- **Dopo**: Il canone medio cresce del 4,2%, trainato dalla domanda logistica.

### 36. Mixed quotation marks
- **Pattern**: `"virgolette"`, `«virgolette»` and `'apici'` in the same document.
- **Why**: inconsistency reads as assembled from several sources.
- **Rewrite**: pick one convention and hold it. House default: double quotes for
  citations, `«` `»` only if the client template uses them.
- **Prima**: Il piano definisce un "quartiere resiliente", con aree «verdi» e zone 'grigie'.
- **Dopo**: Il piano definisce un "quartiere resiliente", con aree "verdi" e zone "grigie".

---

## E. Numbers, units, dates, currency

### 37. Decimal separator
- **Pattern**: `3.7%`, `1,250.00`: the English decimal point.
- **Why**: an Italian reader parses the point as a thousands separator, so the
  magnitude flips.
- **Rewrite**: comma for decimals, always.
- **Prima**: Il tasso è pari a 3.7%.
- **Dopo**: Il tasso è pari al 3,7%.

### 38. Thousands separator
- **Pattern**: `1,250,000` with English commas, or a bare `1250000`.
- **Why**: the English comma inverts the meaning for an Italian reader; the bare
  form is unreadable above four digits.
- **Rewrite**: point as separator, paired with the decimal comma. This is the
  Italian consulting default and what
  `kable(format.args = list(big.mark = ".", decimal.mark = ","))` produces.
  Documented alternative: the thin space (U+202F), for dense tables where a
  point could be misread as a decimal. Pick one per document; never mix.
- **Prima**: 1,250,000 metri quadrati
- **Dopo**: 1.250.000 metri quadrati

### 39. `mln` and `mld`
- **Pattern**: `1.2M`, `3.4B`, `3,4 miliardi di euro` repeated twenty times.
- **Why**: `M` and `B` are English abbreviations; spelling it out every time
  bloats tables.
- **Rewrite**: `mln` and `mld` in tables and figures; spelled out on first use
  in prose.
- **Prima**: Investimenti per 2.4B.
- **Dopo**: Investimenti per 2,4 mld di euro.

### 40. Percent sign spacing
- **Pattern**: `12,4 %`.
- **Why**: Italian typographic practice attaches the sign to the digits; other
  unit symbols instead take a space (`120 m2`, `45 kWh`).
- **Rewrite**: `%` attached; all other units spaced.
- **Prima**: La quota sale al 12,4 % su 120m2.
- **Dopo**: La quota sale al 12,4% su 120 m2.

### 41. Percent versus percentage points
- **Pattern**: `il tasso è cresciuto del 2%` when it moved from 3% to 5%.
- **Why**: a factual error, not a style issue; it understates or overstates by
  an order of magnitude.
- **Rewrite**: `punti percentuali` for a difference of two rates.
- **Prima**: Il tasso di vacancy è salito del 2%.
- **Dopo**: Il tasso di vacancy è salito di 2 punti percentuali, dal 3% al 5%.

### 42. Currency
- **Pattern**: `€1.2M`, `EUR 1,200,000`, `1200000€`.
- **Why**: symbol before the amount is an English convention; the glued symbol
  is a typing artefact.
- **Rewrite**: amount, space, then `euro` in prose or `EUR` in tables. The `€`
  symbol only where the client template uses it, always after the amount.
- **Prima**: Il canone medio è €1.250/mq.
- **Dopo**: Il canone medio è 1.250 euro al metro quadrato.

### 43. Dates
- **Pattern**: `12/03/2026` ambiguous, `12 Marzo 2026` capitalised, `March 2026`.
- **Why**: capital months are English; the numeric form is ambiguous against the
  American order.
- **Rewrite**: `12 marzo 2026` in prose, lowercase month, no ordinal; ISO
  `2026-03-12` in tables and file names.
- **Prima**: Dati aggiornati al 03/12/2026.
- **Dopo**: Dati aggiornati al 12 marzo 2026.

### 44. Ranges
- **Pattern**: a year range joined by the long dash (U+2013), or padded out as
  `dal 2019 fino ad arrivare al 2024`.
- **Why**: rule 06 forbids the en dash in anything we author, and the guard
  rewrites it on write, so the stored file will differ from the draft; the long
  form is padding.
- **Rewrite**: plain hyphen `2019-2024`, or `dal 2019 al 2024`.
- **Prima**: Nel periodo 2019[U+2013]2024 il mercato è raddoppiato.
- **Dopo**: Fra il 2019 e il 2024 il mercato è raddoppiato.

### 45. Spurious precision
- **Pattern**: `il 37,428% degli intervistati` on a sample of 300.
- **Why**: decimals beyond the sampling error claim an accuracy the data cannot
  support; a reviewer will notice before the client does.
- **Rewrite**: round to the precision the method supports and say the sample size.
- **Prima**: Il 37,428% degli intervistati dichiara di rinviare l'acquisto.
- **Dopo**: Il 37% degli intervistati (n = 300) dichiara di rinviare l'acquisto.
