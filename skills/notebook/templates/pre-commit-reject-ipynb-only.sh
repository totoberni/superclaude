
# notebook-skill: reject ipynb without paired py
# Per W1: in paired-mode projects, every .ipynb commit must include the .py change.
# Otherwise the .ipynb diverges from the SoT.
# V3-X4: exempt outputs-only diffs (re-execution doesn't change source).
# UX-4: resolve jupytext via .notebook/jupytext_path captured at `nb init` time
#       (git's pre-commit subshell doesn't inherit the user's .venv PATH).
ipynb_changed=$(git diff --cached --name-only | grep -E '\.ipynb$' || true)
if [ -n "$ipynb_changed" ]; then
    if [ -r ".notebook/jupytext_path" ]; then
        jupytext_bin=$(cat .notebook/jupytext_path)
    else
        jupytext_bin=$(command -v jupytext || echo "")
    fi
fi
for nb in $ipynb_changed; do
    py="${nb%.ipynb}.py"
    if git diff --cached --name-only | grep -qx "$py"; then
        continue
    fi
    # V3-X4: outputs-only check — extract source-only py:percent view and diff
    # against HEAD's paired .py. If equal, .ipynb diff is outputs/metadata only.
    if [ -n "$jupytext_bin" ] && [ -x "$jupytext_bin" ] \
            && git cat-file -e "HEAD:$py" 2>/dev/null; then
        # Port Windows: l'ipynb in stage passa da un file temporaneo e non da
        # stdin (jupytext >= 1.19 rifiuta `-` per i notebook accoppiati, e senza
        # PYTHONUTF8 lo stdin verrebbe decodificato in cp1252). jupytext su
        # Windows scrive CRLF, da cui --strip-trailing-cr nel confronto.
        tmp_dir=$(mktemp -d -t nbskill.XXXXXX)
        trap 'rm -rf "$tmp_dir"' EXIT
        if git show ":$nb" > "$tmp_dir/staged.ipynb" 2>/dev/null \
                && PYTHONUTF8=1 "$jupytext_bin" --to py:percent \
                    -o "$tmp_dir/staged.py" "$tmp_dir/staged.ipynb" >/dev/null 2>&1 \
                && git show "HEAD:$py" > "$tmp_dir/head.py" 2>/dev/null \
                && diff -q --strip-trailing-cr "$tmp_dir/staged.py" "$tmp_dir/head.py" \
                    >/dev/null 2>&1; then
            echo "[notebook] WARN: $nb has outputs-only changes vs HEAD; allowing." >&2
            rm -rf "$tmp_dir"
            trap - EXIT
            continue
        fi
        rm -rf "$tmp_dir"
        trap - EXIT
    fi
    echo "[notebook] REJECT: $nb staged without paired $py" >&2
    echo "[notebook] Run: ~/.claude/skills/notebook/notebook sync $nb && git add $py" >&2
    exit 1
done
