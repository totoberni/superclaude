# Quarto, MiKTeX and the Word reference document

Setup reference for `/deliverable-it`. English (agent facing, rule 07).

Machine state probed 2026-09-22: `quarto` is NOT on PATH, `pandoc` is NOT on
PATH, MiKTeX IS installed at user scope with `xelatex`, `pdflatex` and
`latexmk` under
`%LOCALAPPDATA%\Programs\MiKTeX\miktex\bin\x64` (also not on PATH from Git Bash).

## 1. Installing Quarto at user scope on Windows, no admin

Owner decision; do not install it on your own initiative.

Verified: the Quarto get started page offers two Windows downloads, an
installer named `quarto-<version>-win.msi` and a second entry labelled
"Windows Zip", `quarto-<version>-win.zip`. The GitHub releases API confirms both
assets on the latest release, tag `v1.10.18` as of 2026-09-22
(`quarto-1.10.18-win.msi`, `quarto-1.10.18-win.zip`).

The zip is the no-admin route:

```powershell
$ver = "1.10.18"
$dest = "$env:LOCALAPPDATA\Programs\Quarto"
Invoke-WebRequest "https://github.com/quarto-dev/quarto-cli/releases/download/v$ver/quarto-$ver-win.zip" -OutFile "$env:TEMP\quarto.zip"
Expand-Archive "$env:TEMP\quarto.zip" -DestinationPath $dest -Force
# PATH utente, non di macchina: nessun privilegio amministrativo richiesto
[Environment]::SetEnvironmentVariable("Path", "$([Environment]::GetEnvironmentVariable('Path','User'));$dest\bin", "User")
```

Then open a new shell and run `quarto check`. It reports the bundled pandoc, the
Deno runtime, and whether it found a LaTeX installation and an R or Python
engine. Fix what it reports before debugging a document.

Not verified: that the zip layout places the launcher exactly at `<dest>\bin\quarto.exe`
for this release, and that no per-user post install step is required. Confirm by
listing the extracted tree once. The `.msi` is the supported path and normally
wants elevation; prefer the zip here.

Quarto bundles its own pandoc, so the missing system `pandoc` is not a problem
once Quarto is installed.

## 2. MiKTeX and Italian

PDF goes through `pdf-engine: xelatex`. What the Italian setup needs:

| Need | Package | Engine |
|---|---|---|
| Hyphenation patterns | `hyphen-italian` | all |
| Italian babel locale | `babel-italian` | pdflatex |
| Italian polyglossia locale | `polyglossia` | xelatex, lualatex |
| System font selection | `fontspec`, `unicode-math` | xelatex, lualatex |
| `scrartcl` document class | `koma-script` | all |
| Pandoc default template extras | `geometry`, `xcolor`, `hyperref`, `bookmark`, `caption`, `microtype`, `upquote`, `footnotehyper` | all |

Verified on quarto.org: `lang` identifies the main language with an IETF BCP 47
tag, and it "controls hyphenation in PDF output when using LaTeX (through babel
and polyglossia) or ConTeXt". Italian (`it`) is listed among the languages with a
full bundled translation, so cross reference prefixes, callout labels and the
table of contents heading come out in Italian from `lang: it` alone. Pandoc
derives the babel or polyglossia locale from `lang`; setting `babel-lang` or
`polyglossia-lang` by hand is an override of last resort (commented out in
`templates/_quarto.yml`).

MiKTeX installs missing packages on the fly when auto install is enabled. Turn it
on once for the current user, or pre install the set:

```powershell
$mik = "$env:LOCALAPPDATA\Programs\MiKTeX\miktex\bin\x64"
& "$mik\initexmf" --set-config-value="[MPM]AutoInstall=1"
& "$mik\mpm" --install=hyphen-italian --install=babel-italian --install=polyglossia --install=koma-script
```

Not verified: whether those four packages are already present. A `kpsewhich`
probe from Git Bash on 2026-09-22 did not return within 120 seconds, most likely
a first run package database refresh or an interactive prompt that has no console
to write to. Run the probe once interactively in PowerShell before trusting the
table:

```powershell
& "$mik\kpsewhich" italian.ldf polyglossia.sty fontspec.sty
```

Alternative if MiKTeX interactivity keeps blocking headless renders:
`quarto install tinytex` puts a self contained TeX distribution under the user
profile and installs packages without prompting. It is a second TeX on the
machine; pick one and say which in the report.

Note that the MiKTeX bin directory is not on PATH from Git Bash. Either add it to
the user PATH or let Quarto find it; `quarto check` will say which engine it
resolved.

## 3. The Word reference document

Verified on quarto.org (docx format reference and the Word templates article):

- `reference-doc` means "Use the specified file as a style reference in producing
  a docx, pptx, or odt file".
- The documented way to produce a starting point is

```
quarto pandoc -o custom-reference-doc.docx --print-default-data-file reference.docx
```

  then open that file in Word and modify the styles.
- It is then referenced from the format block:

```yaml
format:
  docx:
    reference-doc: custom-reference-doc.docx
```

In our template the file is named `house-reference.docx` and it is a
placeholder: the owner supplies the real house style file, or the client does.
The path resolves relative to the document (or the project root when set in
`_quarto.yml`). If the file is absent, the render still succeeds with the pandoc
default styling; say so in the report rather than substituting another template.

Styles worth checking in the reference file, because they are what pandoc maps
onto: `Title`, `Subtitle`, `Author`, `Date`, `Heading 1` to `Heading 4`,
`Body Text`, `Table Caption`, `Image Caption`, `Source Code`, `Block Text`. Page
setup, margins, headers and footers are taken from the reference file too.

## 4. One document, Python and R

The template sets `engine: knitr` and loads `reticulate` in the setup chunk.
That is what lets a single `.qmd` hold both a Python chunk and an R chunk: under
the `jupyter` engine only one kernel runs, so R chunks would not execute.

Consequences to remember:

- R must be on PATH (`Rscript`; R 4.6.1 is installed on this machine) and the
  `knitr`, `rmarkdown` and `reticulate` packages must be available.
- `use_virtualenv("../.venv", required = FALSE)` points reticulate at the project
  venv. Set the real relative path per project.
- If the deliverable is Python only, drop `engine: knitr` and let Quarto use the
  jupyter engine; it is lighter and needs no R.

## 5. Common failures

| Symptom | Cause | Fix |
|---|---|---|
| `quarto: command not found` | PATH not refreshed after install | open a new shell; check the user PATH entry |
| English hyphenation in the PDF | `lang` missing or overridden in the qmd front matter | `lang: it` in `_quarto.yml` and not contradicted per document |
| LaTeX error, missing `.sty` | MiKTeX auto install off | run the `mpm --install` line above |
| Render hangs with no output | MiKTeX waiting on an install prompt with no console | enable auto install, or pre install the packages |
| Word file ignores the house style | `reference-doc` path wrong or file absent | path is relative to the document or project root; `quarto render` prints a warning |
| Figures missing in Word but present in PDF | chunk wrote to a path outside the project | keep figures under the project, reference them relatively |
