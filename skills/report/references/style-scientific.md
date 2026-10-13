# Scientific style

> Part of /report (see ../SKILL.md). Mode `--scientific`.

The reader is technical: a data scientist, a statistician, a reviewer. The report
must let them judge whether the result holds and rerun it.

The epistemic standard is `/research`: the Principles in
`~/.claude/skills/research/references/principles.md` (claim provenance,
triangulation, falsifiability, verdict-language calibration) and the LaTeX
conventions in `~/.claude/skills/research/references/report.md` (layered
information, derivations in appendix, notational coherence, `\cref`). This file
covers how those land in the house template.

## Rules

1. **Every number has an uncertainty and a producer.** Mean ± standard deviation over seeds, or a confidence interval with its method; the table or figure that produces it is cited with `\cref`.
2. **Notation is a contract.** Each symbol is defined once, at first use, and keeps that meaning everywhere. For more than about 8 symbols, add a notation table in an appendix.
3. **Equations are numbered and punctuated**, followed by the definition of every symbol not yet introduced.
4. **Baselines are named and fair.** Same splits, same preprocessing, tuned with the same budget.
5. **Hyperparameters are chosen on validation only.** Say how, and with what budget.
6. **Verdict words match the evidence.** "Dimostra" and "conferma" need multiple seeds and a test; one run is "è coerente con" (Principle 14).
7. **Reproducibility is concrete.** Commit or archive version, library versions, seeds, data extraction date and checksum, the command that reruns the experiment.
8. **References are real.** Every `\parencite`/`\textcite` key resolves to a `.bib` entry for a source actually read; DOI or URL when it exists.

## Examples

### A metric with its uncertainty

Bad:

> Il modello raggiunge un'AUC dell'87%, nettamente migliore della baseline.

Good:

> Sul test set (\num{4210} osservazioni) il modello raggiunge un'AUC di \num{0.874 +- 0.006} (media e deviazione standard su 5 seed; IC 95% bootstrap [\num{0.861}; \num{0.887}]), contro \num{0.812 +- 0.009} della regressione logistica (\cref{tab:risultati}).

Rendered: "0,874 ± 0,006". Interval bounds use a semicolon and square brackets, never a dash.

### An equation block

```latex
Sia $\mathcal{D} = \{(x_i, y_i)\}_{i=1}^{n}$ con $x_i \in \R^{d}$ e $y_i \in \{0,1\}$.
I parametri si stimano minimizzando la perdita regolarizzata
\begin{equation}
  \hat{\theta} = \argmin_{\theta} \; \frac{1}{n}\sum_{i=1}^{n}
  \ell\bigl(f_\theta(x_i), y_i\bigr) + \lambda \lVert \theta \rVert_2^2 ,
  \label{eq:obiettivo}
\end{equation}
dove $\ell$ è la log-loss, $f_\theta$ il modello con parametri $\theta$ e
$\lambda \geq 0$ il peso della regolarizzazione, scelto sulla validazione
(\cref{sec:valutazione}).
```

Bad: the same equation with no sentence before it, `\lambda` never defined, and "come si vede nell'equazione sopra" instead of `\cref{eq:obiettivo}`.

### An ablation table

```latex
\begin{tabella}
\begin{xltabular}{\linewidth}{|L|P{4.2cm}|R{2.4cm}|R{1.8cm}|}
\rowcolor{white}\caption{Ablazione: AUC sul test set, media e deviazione standard su 5 seed}\label{tab:ablazione}\\
\hline
\rowcolor{navy}\hd{Variante} & \hd{Componente rimosso} & \hd{AUC} & \hd{$\Delta$}\\ \hline
\endfirsthead
\hline
\rowcolor{navy}\hd{Variante} & \hd{Componente rimosso} & \hd{AUC} & \hd{$\Delta$}\\ \hline
\endhead
Completo & nessuno & \num{0.874 +- 0.006} & \\ \hline
Senza variabili di rete & grado e centralità & \num{0.851 +- 0.007} & \num{-0.023}\\ \hline
Senza regolarizzazione & $\lambda = 0$ & \num{0.866 +- 0.011} & \num{-0.008}\\ \hline
\end{xltabular}
\end{tabella}
```

One component removed per row; $\Delta$ against the full model; the text says
which differences exceed the seed-to-seed spread and which do not.

### Calibrated verdicts

Bad:

> L'esperimento dimostra che le variabili di rete sono la causa del miglioramento.

Good:

> Togliere le variabili di rete riduce l'AUC di \num{0.023}, circa tre volte la deviazione standard tra seed: il risultato è coerente con un contributo reale di queste variabili, ma non distingue tra effetto causale e correlazione con la dimensione dell'impresa (\cref{sec:discussione}).

## Figures

Captions are self-contained: what is plotted, on which data, and what the reader
should conclude. Run `/figure-validate` on every figure before embedding. Figures
float with `[htbp]`; tables do not float (`xltabular` with `\caption`).
