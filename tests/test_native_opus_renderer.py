import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class _Logger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


astrbot_module = types.ModuleType("astrbot")
astrbot_api_module = types.ModuleType("astrbot.api")
astrbot_api_module.logger = _Logger()
astrbot_module.api = astrbot_api_module
sys.modules.setdefault("astrbot", astrbot_module)
sys.modules.setdefault("astrbot.api", astrbot_api_module)

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "services" / "native_opus_renderer.py"
)
SPEC = importlib.util.spec_from_file_location("native_opus_renderer_under_test", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class RenderModeTests(unittest.TestCase):
    def test_explicit_modes_take_priority(self):
        self.assertEqual(MODULE.resolve_render_mode("plain", True), "plain")
        self.assertEqual(MODULE.resolve_render_mode("card", False), "card")
        self.assertEqual(MODULE.resolve_render_mode("native", False), "native")

    def test_auto_and_missing_mode_follow_legacy_rai(self):
        self.assertEqual(MODULE.resolve_render_mode("auto", True), "card")
        self.assertEqual(MODULE.resolve_render_mode("auto", False), "plain")
        self.assertEqual(MODULE.resolve_render_mode(None, True), "card")
        self.assertEqual(MODULE.resolve_render_mode(None, False), "plain")


class CookieMappingTests(unittest.TestCase):
    def test_maps_persisted_credential_names_for_browser(self):
        cookies = MODULE.build_bilibili_cookies(
            {
                "sessdata": "session",
                "bili_jct": "csrf",
                "buvid3": "v3",
                "buvid4": "v4",
                "dedeuserid": "42",
                "ac_time_value": "refresh",
            }
        )
        self.assertEqual(
            {cookie["name"] for cookie in cookies},
            {
                "SESSDATA",
                "bili_jct",
                "buvid3",
                "buvid4",
                "DedeUserID",
                "ac_time_value",
            },
        )
        self.assertTrue(all(cookie["domain"] == ".bilibili.com" for cookie in cookies))
        self.assertTrue(all(cookie["secure"] for cookie in cookies))

    def test_ignores_empty_and_unknown_credential_fields(self):
        cookies = MODULE.build_bilibili_cookies(
            {"sessdata": "", "bili_jct": None, "unknown": "secret"}
        )
        self.assertEqual(cookies, [])


class BrowserRuntimeTests(unittest.TestCase):
    def test_browser_timeout_has_sixty_second_floor(self):
        renderer = MODULE.NativeOpusRenderer(lambda: {}, timeout_secs=30)
        self.assertEqual(renderer.timeout_ms, 60000)

    def test_video_dynamic_uses_card_renderer(self):
        self.assertFalse(MODULE.supports_native_render("DYNAMIC_TYPE_AV"))
        self.assertTrue(MODULE.supports_native_render("DYNAMIC_TYPE_DRAW"))

    def test_resolves_actual_bilibili_page_url(self):
        self.assertEqual(
            MODULE.resolve_bilibili_page_url(
                "https://www.bilibili.com/read/cv123", "456"
            ),
            "https://www.bilibili.com/read/cv123",
        )
        self.assertEqual(
            MODULE.resolve_bilibili_page_url("//www.bilibili.com/opus/789", "456"),
            "https://www.bilibili.com/opus/789",
        )

    def test_rejects_external_page_url(self):
        self.assertEqual(
            MODULE.resolve_bilibili_page_url("https://example.com/unsafe", "456"),
            "https://www.bilibili.com/opus/456",
        )

    def test_builds_authenticated_remote_browser_url(self):
        self.assertEqual(
            MODULE.build_remote_browser_url(
                "ws://browserless:3000?existing=value", "secret token"
            ),
            "ws://browserless:3000?existing=value&token=secret+token",
        )

    def test_rejects_invalid_remote_browser_url(self):
        with self.assertRaisesRegex(ValueError, "Browserless 地址"):
            MODULE.build_remote_browser_url("browserless:3000", "secret")


class _FakeChromium:
    def __init__(self):
        self.endpoint = ""
        self.timeout = 0

    async def connect_over_cdp(self, endpoint, *, timeout):
        self.endpoint = endpoint
        self.timeout = timeout
        return _FakeBrowser()


class _FakePlaywright:
    def __init__(self):
        self.chromium = _FakeChromium()

    async def stop(self):
        return None


class _FakePlaywrightStarter:
    def __init__(self, playwright):
        self.playwright = playwright

    async def start(self):
        return self.playwright


class _FakeBrowserContext:
    pass


class _FakeBrowser:
    def __init__(self):
        self.context_options = None

    def is_connected(self):
        return True

    async def new_context(self, **options):
        self.context_options = options
        return _FakeBrowserContext()


class BrowserContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_connects_to_browserless_over_cdp(self):
        playwright = _FakePlaywright()
        async_api_module = types.ModuleType("playwright.async_api")
        async_api_module.async_playwright = lambda: _FakePlaywrightStarter(playwright)
        playwright_module = types.ModuleType("playwright")
        playwright_module.async_api = async_api_module
        renderer = MODULE.NativeOpusRenderer(
            lambda: {},
            remote_browser_url="ws://browserless:3000",
            remote_browser_token="secret token",
        )

        with patch.dict(
            sys.modules,
            {
                "playwright": playwright_module,
                "playwright.async_api": async_api_module,
            },
        ):
            self.assertTrue(await renderer._start_browser())

        self.assertEqual(
            playwright.chromium.endpoint,
            "ws://browserless:3000?token=secret+token",
        )
        self.assertEqual(playwright.chromium.timeout, 60000)

    async def test_uses_full_hd_viewport_without_pixel_upscaling(self):
        renderer = MODULE.NativeOpusRenderer(lambda: {})
        browser = _FakeBrowser()
        renderer._browser = browser

        await renderer._ensure_context()

        self.assertEqual(
            browser.context_options["viewport"],
            {"width": 1920, "height": 1080},
        )
        self.assertEqual(browser.context_options["device_scale_factor"], 1)

    async def test_applies_proxy_to_remote_browser_context(self):
        renderer = MODULE.NativeOpusRenderer(
            lambda: {}, proxy="http://proxy:7890"
        )
        browser = _FakeBrowser()
        renderer._browser = browser

        await renderer._ensure_context()

        self.assertEqual(
            browser.context_options["proxy"], {"server": "http://proxy:7890"}
        )


class _FakeContainer:
    async def wait_for(self, **_kwargs):
        return None

    async def bounding_box(self):
        return {"width": 708, "height": 320}

    async def screenshot(self, *, path, **_kwargs):
        with open(path, "wb") as output:
            output.write(b"x" * 5000)


class _FakeLocator:
    def __init__(self, container):
        self.first = container


class _FakePage:
    def __init__(self):
        self.container = _FakeContainer()
        self.goto_url = ""
        self.closed = False
        self.evaluate_calls = []

    async def goto(self, url, **_kwargs):
        self.goto_url = url

    def locator(self, selector):
        assert selector == MODULE.OPUS_SELECTOR
        return _FakeLocator(self.container)

    async def evaluate(self, expression, arg=None):
        self.evaluate_calls.append((expression, arg))
        return None

    async def wait_for_function(self, *_args, **_kwargs):
        return None

    async def close(self):
        self.closed = True


class _FakeContext:
    def __init__(self):
        self.page = _FakePage()

    async def new_page(self):
        return self.page


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_uses_actual_url_and_hides_page_overlays(self):
        renderer = MODULE.NativeOpusRenderer(lambda: {})
        context = _FakeContext()
        page_url = "https://www.bilibili.com/opus/123456"
        output_path = await renderer._capture(context, "123456", page_url)

        self.assertEqual(context.page.goto_url, page_url)
        self.assertTrue(
            any(
                arg == list(MODULE.OBSTRUCTIVE_PAGE_SELECTORS)
                and "element.remove()" in expression
                for expression, arg in context.page.evaluate_calls
            )
        )
        self.assertTrue(
            any(
                arg == MODULE.OPUS_SELECTOR and "window.scrollTo" in expression
                for expression, arg in context.page.evaluate_calls
            )
        )
        self.assertTrue(context.page.closed)
        self.assertTrue(output_path and os.path.exists(output_path))
        self.assertIn(output_path, renderer._output_paths)

        renderer.release_output(output_path)
        self.assertFalse(os.path.exists(output_path))

    async def test_invalid_dynamic_id_does_not_open_browser(self):
        renderer = MODULE.NativeOpusRenderer(lambda: {})
        self.assertIsNone(await renderer.render_dynamic("not-a-number"))


if __name__ == "__main__":
    unittest.main()
