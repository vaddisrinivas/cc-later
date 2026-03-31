import importlib.util
from pathlib import Path
import sys


def load_handler_module():
    repo_root = Path(__file__).resolve().parents[1]
    handler_path = repo_root / "scripts" / "handler.py"
    if not handler_path.exists():
        raise FileNotFoundError(f"handler.py not found at {handler_path}")

    spec = importlib.util.spec_from_file_location("cc_later_handler", handler_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not create import spec for {handler_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
