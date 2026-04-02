import sys
import unittest
from io import StringIO
from unittest.mock import patch

from tests._loader import load_handler_module


class DryRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handler = load_handler_module()

    def _invoke_dry_run(self) -> str:
        with patch.object(sys, "argv", ["handler.py", "--dry-run"]), patch(
            "sys.stdout", new_callable=StringIO
        ) as mock_out:
            self.handler.main()
        return mock_out.getvalue()

    def test_dry_run_prints_header(self):
        output = self._invoke_dry_run()
        self.assertIn("[cc-later --dry-run]", output)

    def test_dry_run_shows_gate_checks(self):
        output = self._invoke_dry_run()
        # New format uses [pass] / [FAIL] instead of Gate:
        self.assertIn("dispatch.enabled", output)
        self.assertTrue("[pass]" in output or "[FAIL]" in output)

    def test_dry_run_does_not_spawn(self):
        with patch.object(sys, "argv", ["handler.py", "--dry-run"]), patch(
            "cc_later.dispatcher._spawn_dispatch"
        ) as mock_spawn, patch("sys.stdout", new_callable=StringIO):
            self.handler.main()
        mock_spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
