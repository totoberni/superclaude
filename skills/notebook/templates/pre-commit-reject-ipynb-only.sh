
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
        tmp_staged=$(mktemp -t nbskill-staged.XXXXXX.py)
        tmp_head=$(mktemp -t nbskill-head.XXXXXX.py)
        trap 'rm -f "$tmp_staged" "$tmp_head"' EXIT
        if git show ":$nb" 2>/dev/null \
                | "$jupytext_bin" --to py:percent --from ipynb -o "$tmp_staged" - \
                    >/dev/null 2>&1 \
                && git show "HEAD:$py" > "$tmp_head" 2>/dev/null \
                && diff -q "$tmp_staged" "$tmp_head" >/dev/null 2>&1; then
            echo "[notebook] WARN: $nb has outputs-only changes vs HEAD; allowing." >&2
            rm -f "$tmp_staged" "$tmp_head"
            trap - EXIT
            continue
        fi
        rm -f "$tmp_staged" "$tmp_head"
        trap - EXIT
    fi
    echo "[notebook] REJECT: $nb staged without paired $py" >&2
    echo "[notebook] Run: ~/.claude/skills/notebook/notebook sync $nb && git add $py" >&2
    exit 1
done
