"""Find the shared runtime in a release package or the source checkout."""
import importlib
from pathlib import Path
import sys


def load_runtime(module, root=None):
    scripts = Path(__file__).resolve().parent
    candidates = [Path(root)] if root else [scripts / "runtime", scripts.parents[2] / "automation"]
    runtime = next((p.resolve() for p in candidates if (p / (module + ".py")).is_file()), None)
    if runtime is None:
        raise ValueError("shared runtime is missing; use the release ZIP or --runtime-root <automation>")
    existing = sys.modules.get(module)
    if existing is not None and Path(existing.__file__).resolve().parent != runtime:
        raise ValueError("a different runtime is already loaded; start a fresh process")
    sys.path.insert(0, str(runtime))
    return importlib.import_module(module)
