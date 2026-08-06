import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "core" / "models.py"
SPEC = importlib.util.spec_from_file_location("models_under_test", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class RenderPayloadTests(unittest.TestCase):
    def test_publication_time_round_trips(self):
        payload = MODULE.RenderPayload.from_dict(
            {
                "name": "测试用户",
                "pub_time": "刚刚",
                "forward": {"name": "来源用户", "pub_time": "昨天"},
            }
        )

        serialized = payload.to_dict()
        self.assertEqual(serialized["pub_time"], "刚刚")
        self.assertEqual(serialized["forward"]["pub_time"], "昨天")

    def test_forward_payload_preserves_publication_time(self):
        payload = MODULE.RenderPayload(name="来源用户", pub_time="5分钟前")

        forward = payload.to_forward_payload()

        self.assertEqual(forward.pub_time, "5分钟前")

    def test_article_content_blocks_round_trip(self):
        payload = MODULE.RenderPayload.from_dict(
            {
                "content_blocks": [
                    {"kind": "text", "text": "正文", "align": "center"},
                    {
                        "kind": "images",
                        "image_urls": ["https://example.com/1.jpg"],
                        "align": "invalid",
                    },
                ]
            }
        )

        serialized = payload.to_dict()["content_blocks"]
        self.assertEqual(serialized[0]["text"], "正文")
        self.assertEqual(serialized[0]["align"], "center")
        self.assertEqual(serialized[1]["align"], "left")
        self.assertEqual(
            serialized[1]["image_urls"], ["https://example.com/1.jpg"]
        )


if __name__ == "__main__":
    unittest.main()
