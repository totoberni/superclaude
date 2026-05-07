# <project>/.notebook/warm.py — kernel pre-import config (V1.1: auto-generated).
#
# This template file is shipped for reference; it is NOT what `nb init` copies
# at runtime. `nb_init._generate_warm_py` synthesises the actual warm.py from
# the in-code `_WARM_BASE` constant + presets (qiskit, torch, scipy, sklearn)
# detected via `pip list` against the project's `.venv/`.
#
# Re-run `nb init --migrate --force` to regenerate based on currently-installed
# packages. Hand-edits are preserved unless the existing file matches the v1
# commented signature OR the v2 auto-generated signature.
#
# Each import block is wrapped in try/except — failures log but do not abort.

try:
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    print("[warm] numpy + matplotlib ready")
except Exception as e:
    print(f"[warm] numpy/matplotlib failed: {e}")

# Auto-detected blocks added by `nb init` based on `pip list`. Examples below
# are illustrative — the running version of this file inside a project will
# have only the relevant blocks.

# Example: qiskit preset (added if `qiskit` AND `qiskit_aer` are installed)
# try:
#     import qiskit
#     import qiskit_aer
#     from qiskit_aer import AerSimulator
#     _ = AerSimulator()  # forces backend instantiation, caches BLAS
#     print(f"[warm] qiskit {qiskit.__version__} + Aer ready")
# except Exception as e:
#     print(f"[warm] qiskit failed: {e}")

# Example: torch preset (added if `torch` is installed)
# try:
#     import torch
#     print(f"[warm] torch {torch.__version__} cuda={torch.cuda.is_available()}")
# except Exception as e:
#     print(f"[warm] torch failed: {e}")

# Example: scipy/sklearn preset (added if either is installed)
# try:
#     import scipy
#     print(f"[warm] scipy {scipy.__version__}")
# except Exception as e:
#     print(f"[warm] scipy failed: {e}")
# try:
#     import sklearn
#     print(f"[warm] sklearn {sklearn.__version__}")
# except Exception as e:
#     print(f"[warm] sklearn failed: {e}")
