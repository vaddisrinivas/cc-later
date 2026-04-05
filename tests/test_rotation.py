import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from tests._loader import load_handler_module


class RotationTests(unittest.TestCase):
    TZ = ZoneInfo("America/New_York")
    FIXED_NOW = datetime(2026, 4, 5, 12, 0, tzinfo=TZ)

    @classmethod
    def setUpClass(cls):
        cls.handler = load_handler_module()

    def _make_later(
        self,
        tmpdir: str,
        content: str,
        mtime: datetime | None = None,
    ) -> Path:
        later = Path(tmpdir) / ".claude" / "LATER.md"
        later.parent.mkdir(parents=True, exist_ok=True)
        later.write_text(content, encoding="utf-8")
        if mtime is not None:
            ts = mtime.timestamp()
            os.utime(later, (ts, ts))
        return later

    def test_rotates_when_mtime_is_yesterday(self):
        now = self.FIXED_NOW
        with tempfile.TemporaryDirectory() as td:
            content = "# LATER\n\n- [ ] pending task\n- [x] done task\n"
            later = self._make_later(td, content, mtime=now - timedelta(days=1))
            rotated = self.handler.rotate_later_if_needed(later, now)
        self.assertTrue(rotated)

    def test_no_rotation_when_mtime_is_today(self):
        now = self.FIXED_NOW
        with tempfile.TemporaryDirectory() as td:
            content = "# LATER\n\n- [ ] pending task\n"
            later = self._make_later(td, content, mtime=now - timedelta(hours=1))
            rotated = self.handler.rotate_later_if_needed(later, now)
        self.assertFalse(rotated)

    def test_archive_file_created_with_full_content(self):
        now = self.FIXED_NOW
        with tempfile.TemporaryDirectory() as td:
            content = "# LATER\n\n- [ ] pending\n- [x] done\n"
            yesterday = (now - timedelta(days=1)).date()
            later = self._make_later(td, content, mtime=now - timedelta(days=1))
            archive_path = later.parent / f"LATER-{yesterday.isoformat()}.md"
            self.handler.rotate_later_if_needed(later, now)
            self.assertTrue(archive_path.exists())
            archived = archive_path.read_text(encoding="utf-8")
            self.assertIn("pending", archived)
            self.assertIn("done", archived)

    def test_new_later_md_has_only_pending_entries(self):
        now = self.FIXED_NOW
        with tempfile.TemporaryDirectory() as td:
            content = "# LATER\n\n- [ ] keep this\n- [x] discard done\n- [!] keep urgent\n"
            later = self._make_later(td, content, mtime=now - timedelta(days=1))
            self.handler.rotate_later_if_needed(later, now)
            fresh = later.read_text(encoding="utf-8")
            self.assertIn("keep this", fresh)
            self.assertIn("keep urgent", fresh)
            self.assertNotIn("discard done", fresh)

    def test_sections_preserved_in_fresh_file(self):
        now = self.FIXED_NOW
        with tempfile.TemporaryDirectory() as td:
            content = (
                "# LATER\n\n"
                "## Tests\n- [ ] add tests\n- [x] done test\n\n"
                "## Reports\n- [ ] generate report\n"
            )
            later = self._make_later(td, content, mtime=now - timedelta(days=1))
            self.handler.rotate_later_if_needed(later, now)
            fresh = later.read_text(encoding="utf-8")
            self.assertIn("## Tests", fresh)
            self.assertIn("add tests", fresh)
            self.assertNotIn("done test", fresh)
            self.assertIn("## Reports", fresh)
            self.assertIn("generate report", fresh)

    def test_all_done_file_produces_empty_fresh_file(self):
        now = self.FIXED_NOW
        with tempfile.TemporaryDirectory() as td:
            content = "# LATER\n\n- [x] all done\n- [x] also done\n"
            later = self._make_later(td, content, mtime=now - timedelta(days=1))
            self.handler.rotate_later_if_needed(later, now)
            fresh = later.read_text(encoding="utf-8")
            self.assertNotIn("all done", fresh)
            self.assertNotIn("also done", fresh)
            # Should still be a valid file with just the header
            self.assertIn("# LATER", fresh)

    def test_no_rotation_when_file_missing(self):
        now = self.FIXED_NOW
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / ".claude" / "LATER.md"
            rotated = self.handler.rotate_later_if_needed(missing, now)
        self.assertFalse(rotated)

    def test_empty_file_rotates_to_header_only(self):
        now = self.FIXED_NOW
        with tempfile.TemporaryDirectory() as td:
            later = self._make_later(td, "", mtime=now - timedelta(days=1))
            rotated = self.handler.rotate_later_if_needed(later, now)
            self.assertTrue(rotated)
            fresh = later.read_text(encoding="utf-8")
            self.assertEqual(fresh, "# LATER\n")

    def test_section_with_only_done_entries_omitted_from_fresh_file(self):
        now = self.FIXED_NOW
        with tempfile.TemporaryDirectory() as td:
            content = (
                "# LATER\n\n"
                "## Done Section\n- [x] finished a\n- [x] finished b\n\n"
                "## Pending Section\n- [ ] still todo\n"
            )
            later = self._make_later(td, content, mtime=now - timedelta(days=1))
            self.handler.rotate_later_if_needed(later, now)
            fresh = later.read_text(encoding="utf-8")
            self.assertNotIn("Done Section", fresh)
            self.assertNotIn("finished a", fresh)
            self.assertIn("Pending Section", fresh)
            self.assertIn("still todo", fresh)

    def test_rotation_uses_now_timezone_for_mtime_date(self):
        now = datetime(2026, 4, 5, 0, 30, tzinfo=self.TZ)
        # 03:30 UTC on Apr 5 is 23:30 ET on Apr 4 (yesterday relative to now).
        mtime_utc = datetime(2026, 4, 5, 3, 30, tzinfo=ZoneInfo("UTC"))
        with tempfile.TemporaryDirectory() as td:
            later = self._make_later(td, "# LATER\n\n- [ ] task\n", mtime=mtime_utc)
            rotated = self.handler.rotate_later_if_needed(later, now)
            self.assertTrue(rotated)


if __name__ == "__main__":
    unittest.main()
