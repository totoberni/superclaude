# ---- runtime probe (single source of truth — emitted by /notebook init) ----
# Per W5: prefer `"google.colab" in sys.modules` over `try/except import` —
# no import side-effect, no ~50 ms compile cost.
import os, sys
from pathlib import Path

if "google.colab" in sys.modules:
    RUNTIME = "colab"
elif "KAGGLE_KERNEL_RUN_TYPE" in os.environ:
    RUNTIME = "kaggle"
elif os.environ.get("CODESPACES") == "true":
    RUNTIME = "codespaces"
elif "BINDER_SERVICE_HOST" in os.environ:
    RUNTIME = "binder"
elif "JUPYTERHUB_USER" in os.environ:
    RUNTIME = "jupyterhub"
else:
    RUNTIME = "local"

IS_COLAB = (RUNTIME == "colab")

# Headless rendering for nbconvert --execute / agent-side validation.
os.environ.setdefault("MPLBACKEND", "Agg")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def project_path(rel: str) -> Path:
    """Resolve a project-relative path correctly per runtime."""
    if RUNTIME == "colab":
        from google.colab import drive
        if not Path("/content/drive").is_mount():
            drive.mount("/content/drive")
        return Path("/content/drive/MyDrive") / rel
    if RUNTIME == "kaggle":
        return Path("/kaggle/working") / rel
    return Path.cwd() / rel


print(f"[notebook] runtime={RUNTIME}")
