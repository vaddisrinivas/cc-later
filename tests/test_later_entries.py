import unittest

from tests._loader import load_handler_module


class LaterEntryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handler = load_handler_module()

    def test_priority_entries_are_selected_first(self):
        content = """# LATER
- [ ] normal one
- [!] urgent one
- [ ] normal two
- [x] done item
"""
        entries = self.handler.parse_later_entries(content, priority_marker="[!]")
        selected = self.handler.select_entries(entries, max_entries=3)

        self.assertEqual([e.text for e in selected], ["urgent one", "normal one", "normal two"])

    def test_ids_are_deterministic_and_unique_for_duplicates(self):
        content = """- [ ] duplicate task
- [ ] duplicate task
"""
        entries_first = self.handler.parse_later_entries(content, priority_marker="[!]")
        entries_second = self.handler.parse_later_entries(content, priority_marker="[!]")

        ids_first = [e.id for e in entries_first]
        ids_second = [e.id for e in entries_second]

        self.assertEqual(ids_first, ids_second)
        self.assertEqual(len(set(ids_first)), 2)


if __name__ == "__main__":
    unittest.main()
