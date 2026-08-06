import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "core" / "constant.py"
SPEC = importlib.util.spec_from_file_location("constant_under_test", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class RenderModeTests(unittest.TestCase):
    def test_explicit_modes(self):
        self.assertEqual(MODULE.resolve_render_mode("plain", True), "plain")
        self.assertEqual(MODULE.resolve_render_mode("card", False), "card")

    def test_legacy_modes(self):
        self.assertEqual(MODULE.resolve_render_mode("auto", True), "card")
        self.assertEqual(MODULE.resolve_render_mode("auto", False), "plain")
        self.assertEqual(MODULE.resolve_render_mode("native", False), "card")


if __name__ == "__main__":
    unittest.main()
