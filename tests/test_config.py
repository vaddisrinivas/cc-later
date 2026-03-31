import unittest

from tests._loader import load_handler_module


class ConfigValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handler = load_handler_module()

    def test_accepts_minimal_valid_config(self):
        cfg = self.handler.validate_config_dict(
            {
                "paths": {"watch": ["~/projects/my-repo"]},
                "dispatch": {"enabled": True},
            }
        )
        self.assertTrue(cfg.dispatch.enabled)
        self.assertEqual(cfg.paths.watch, ["~/projects/my-repo"])
        self.assertEqual(cfg.window.dispatch_mode, "window_aware")

    def test_rejects_unknown_top_level_key(self):
        with self.assertRaises(self.handler.ConfigError):
            self.handler.validate_config_dict(
                {
                    "paths": {"watch": ["~/projects/my-repo"]},
                    "dispatch": {"enabled": True},
                    "unknown": {"foo": "bar"},
                }
            )

    def test_rejects_unknown_nested_key(self):
        with self.assertRaises(self.handler.ConfigError):
            self.handler.validate_config_dict(
                {
                    "paths": {"watch": ["~/projects/my-repo"]},
                    "dispatch": {"enabled": True},
                    "window": {"dispatch_mode": "always", "bogus": True},
                }
            )


if __name__ == "__main__":
    unittest.main()
