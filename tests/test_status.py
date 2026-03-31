import importlib.util
import sys
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch


def load_status_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "status.py"
    name = "cc_later_status"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load status.py from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class StatusCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.status = load_status_module()

    def _run_status(self) -> tuple[int, str]:
        with patch("sys.stdout", new_callable=StringIO) as mock_out:
            code = self.status.main()
        return code, mock_out.getvalue()

    def test_status_exits_successfully(self):
        code, _ = self._run_status()
        self.assertEqual(code, 0)

    def test_status_has_required_sections(self):
        _, output = self._run_status()
        for section in (
            "## cc-later Status",
            "### Window",
            "### Gates",
            "### Queue",
            "### Recent Runs",
        ):
            self.assertIn(section, output, f"Missing section: {section}")

    def test_status_shows_dispatch_gate(self):
        _, output = self._run_status()
        self.assertIn("dispatch.enabled", output)


if __name__ == "__main__":
    unittest.main()
