import tempfile
import unittest
from pathlib import Path

from tests._loader import load_handler_module


class LockingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handler = load_handler_module()

    def test_non_blocking_file_lock(self):
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "handler.lock"
            first = self.handler.NonBlockingFileLock(lock_path)
            second = self.handler.NonBlockingFileLock(lock_path)

            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())

            first.release()
            self.assertTrue(second.acquire())
            second.release()


if __name__ == "__main__":
    unittest.main()
