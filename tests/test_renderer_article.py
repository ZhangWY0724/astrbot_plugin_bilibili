import sys
import types
import unittest
from pathlib import Path


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


astrbot = types.ModuleType("astrbot")
astrbot_api = types.ModuleType("astrbot.api")
astrbot_api.logger = _Logger()
astrbot_all = types.ModuleType("astrbot.api.all")
astrbot_all.Star = object
astrbot.api = astrbot_api
sys.modules.setdefault("astrbot", astrbot)
sys.modules.setdefault("astrbot.api", astrbot_api)
sys.modules.setdefault("astrbot.api.all", astrbot_all)

REPO_PARENT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_PARENT))

utils_module = types.ModuleType("astrbot_plugin_bilibili.core.utils")
utils_module.create_qrcode = lambda _value: ""
utils_module.image_to_base64 = lambda _value: ""
utils_module.parse_rich_text = lambda *_args: ""
sys.modules.setdefault("astrbot_plugin_bilibili.core.utils", utils_module)

from astrbot_plugin_bilibili.core.models import RenderPayload
from astrbot_plugin_bilibili.services.renderer import Renderer


class ArticleRendererTests(unittest.TestCase):
    def test_builds_ordered_article_blocks_and_escapes_text(self):
        detail = {
            "item": {
                "modules": [
                    {
                        "module_content": {
                            "paragraphs": [
                                {
                                    "para_type": 1,
                                    "align": 1,
                                    "text": {
                                        "nodes": [
                                            {
                                                "word": {
                                                    "words": "<正文>",
                                                    "style": {"bold": True},
                                                }
                                            },
                                            {
                                                "rich": {
                                                    "text": "链接",
                                                    "jump_url": "//www.bilibili.com/opus/1",
                                                }
                                            },
                                        ]
                                    },
                                },
                                {
                                    "para_type": 2,
                                    "align": 0,
                                    "pic": {
                                        "pics": [
                                            {"url": "http://i0.hdslb.com/a.jpg"},
                                            {
                                                "url": "https://i0.hdslb.com/b.jpg",
                                                "width": 2160,
                                            },
                                        ]
                                    },
                                },
                            ]
                        }
                    }
                ]
            }
        }

        blocks = Renderer.build_article_content_blocks(detail)

        self.assertEqual([block.kind for block in blocks], ["text", "images"])
        self.assertEqual(blocks[0].align, "center")
        self.assertIn("<strong>&lt;正文&gt;</strong>", blocks[0].text)
        self.assertIn('href="https://www.bilibili.com/opus/1"', blocks[0].text)
        self.assertEqual(
            blocks[1].image_urls,
            [
                "https://i0.hdslb.com/a.jpg",
                "https://i0.hdslb.com/b.jpg@1020w.webp",
            ],
        )

    def test_enrich_keeps_empty_detail_as_fallback(self):
        renderer = Renderer.__new__(Renderer)
        payload = RenderPayload(text="摘要", image_urls=["cover.jpg"])

        renderer.enrich_article_payload(payload, None)

        self.assertEqual(payload.content_blocks, [])
        self.assertEqual(payload.text, "摘要")
        self.assertEqual(payload.image_urls, ["cover.jpg"])


if __name__ == "__main__":
    unittest.main()
