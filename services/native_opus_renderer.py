from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Callable
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from astrbot.api import logger

OPUS_URL_TEMPLATE = "https://www.bilibili.com/opus/{dyn_id}"
OPUS_SELECTOR = ".bili-opus-view"
OBSTRUCTIVE_PAGE_SELECTORS = (
    ".v-popover-content",
    ".bili-header__bar.mini-header",
)
VIDEO_DYNAMIC_TYPE = "DYNAMIC_TYPE_AV"
NATIVE_VIEWPORT_WIDTH = 1920
NATIVE_VIEWPORT_HEIGHT = 1080
NATIVE_DEVICE_SCALE_FACTOR = 1
REMOTE_BROWSER_SCHEMES = {"http", "https", "ws", "wss"}


def resolve_render_mode(render_mode: Any, legacy_rai: Any = True) -> str:
    """解析三态渲染配置，并兼容旧版 rai 布尔配置。"""
    normalized = str(render_mode or "").strip().lower()
    if normalized in {"plain", "card", "native"}:
        return normalized
    return "card" if bool(legacy_rai) else "plain"


def build_bilibili_cookies(credential: Optional[Dict[str, Any]]) -> list[dict]:
    """将 bilibili-api-python 凭据转换为网页 Cookie。"""
    if not credential:
        return []

    cookie_names = {
        "sessdata": "SESSDATA",
        "bili_jct": "bili_jct",
        "buvid3": "buvid3",
        "buvid4": "buvid4",
        "dedeuserid": "DedeUserID",
        "ac_time_value": "ac_time_value",
    }
    cookies = []
    for source_name, browser_name in cookie_names.items():
        value = credential.get(source_name)
        if value is None or str(value) == "":
            continue
        cookies.append(
            {
                "name": browser_name,
                "value": str(value),
                "domain": ".bilibili.com",
                "path": "/",
                "secure": True,
                "sameSite": "Lax",
            }
        )
    return cookies


def supports_native_render(dynamic_type: Any) -> bool:
    """视频投稿继续使用现有卡片，其他动态允许原生页面截图。"""
    return str(dynamic_type or "") != VIDEO_DYNAMIC_TYPE


def resolve_bilibili_page_url(target_url: Any, dyn_id: str) -> str:
    """只接受 Bilibili HTTPS 页面地址，无效时回退到动态 opus 地址。"""
    fallback = OPUS_URL_TEMPLATE.format(dyn_id=dyn_id)
    candidate = str(target_url or "").strip()
    if candidate.startswith("//"):
        candidate = f"https:{candidate}"
    try:
        parsed = urlparse(candidate)
        hostname = (parsed.hostname or "").lower()
    except ValueError:
        return fallback
    if (
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and (hostname == "bilibili.com" or hostname.endswith(".bilibili.com"))
    ):
        return candidate
    return fallback


def build_remote_browser_url(endpoint: Any, token: Any = "") -> str:
    """校验 Browserless 地址，并按需追加鉴权 Token。"""
    candidate = str(endpoint or "").strip()
    parsed = urlsplit(candidate)
    if parsed.scheme.lower() not in REMOTE_BROWSER_SCHEMES or not parsed.hostname:
        raise ValueError("Browserless 地址必须是有效的 http(s) 或 ws(s) URL")

    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    normalized_token = str(token or "").strip()
    if normalized_token:
        query["token"] = normalized_token
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


class NativeOpusRenderer:
    """使用 Playwright 截取 Bilibili 原生动态容器。"""

    def __init__(
        self,
        credential_provider: Callable[[], Optional[Dict[str, Any]]],
        proxy: str = "",
        remote_browser_url: str = "",
        remote_browser_token: str = "",
        timeout_secs: float = 60,
    ) -> None:
        self.credential_provider = credential_provider
        self.proxy = (proxy or "").strip()
        self.remote_browser_url = str(remote_browser_url or "").strip()
        self.remote_browser_token = str(remote_browser_token or "").strip()
        self.timeout_ms = max(int(float(timeout_secs) * 1000), 60000)

        self._lock = asyncio.Lock()
        self._playwright = None
        self._browser = None
        self._context = None
        self._credential_fingerprint: tuple[tuple[str, str], ...] = ()
        self._output_paths: set[str] = set()
        self._disabled_reason = ""

    async def render_dynamic(
        self, dyn_id: str, target_url: Optional[str] = None
    ) -> Optional[str]:
        """访问动态实际页面并截取主体，失败时返回 None。"""
        normalized_id = str(dyn_id or "").strip()
        if not normalized_id.isdigit():
            logger.warning(f"原生动态渲染跳过无效动态 ID: {normalized_id!r}")
            return None
        page_url = resolve_bilibili_page_url(target_url, normalized_id)

        async with self._lock:
            try:
                context = await self._ensure_context()
                if context is None:
                    return None
                return await self._capture(context, normalized_id, page_url)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    f"Bilibili 原生动态渲染失败: dyn_id={normalized_id} error={exc}"
                )
                return None

    async def prepare_browser(self) -> bool:
        """在插件启动阶段连接 Browserless。"""
        async with self._lock:
            try:
                if self._browser is not None and self._browser.is_connected():
                    return True
                await self._reset_browser_objects()
                return await self._start_browser()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"插件启动阶段连接 Browserless 失败: {exc}")
                return False

    async def _ensure_context(self):
        credentials = self.credential_provider() or {}
        fingerprint = tuple(
            sorted((str(key), str(value)) for key, value in credentials.items() if value)
        )

        browser_connected = self._browser is not None and self._browser.is_connected()
        if not browser_connected:
            await self._reset_browser_objects()
            started = await self._start_browser()
            if not started:
                return None

        if self._context is not None and fingerprint == self._credential_fingerprint:
            return self._context

        if self._context is not None:
            await self._context.close()

        context_options: Dict[str, Any] = {
            "viewport": {
                "width": NATIVE_VIEWPORT_WIDTH,
                "height": NATIVE_VIEWPORT_HEIGHT,
            },
            "device_scale_factor": NATIVE_DEVICE_SCALE_FACTOR,
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "color_scheme": "light",
        }
        if self.proxy:
            context_options["proxy"] = {"server": self.proxy}
        self._context = await self._browser.new_context(**context_options)
        cookies = build_bilibili_cookies(credentials)
        if cookies:
            await self._context.add_cookies(cookies)
        self._credential_fingerprint = fingerprint
        return self._context

    async def _start_browser(self) -> bool:
        if self._disabled_reason:
            return False

        if not self.remote_browser_url:
            self._disabled_reason = "未配置 Browserless 地址"
            logger.error("原生动态渲染不可用：请配置 Browserless 地址。")
            return False

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self._disabled_reason = "playwright Python 依赖未安装"
            logger.error(
                "原生动态渲染不可用：playwright 未安装，请重新安装插件依赖。"
            )
            return False

        endpoint = ""
        try:
            endpoint = build_remote_browser_url(
                self.remote_browser_url, self.remote_browser_token
            )
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.connect_over_cdp(
                endpoint, timeout=self.timeout_ms
            )
            logger.info("Bilibili 原生动态已连接 Browserless。")
            return True
        except asyncio.CancelledError:
            await self._stop_playwright_driver()
            raise
        except Exception as exc:
            await self._stop_playwright_driver()
            error = str(exc)
            for sensitive_value in (
                endpoint,
                self.remote_browser_token,
                self.remote_browser_url,
                self.proxy,
            ):
                if sensitive_value:
                    error = error.replace(sensitive_value, "[敏感配置已隐藏]")
            logger.error(
                "连接 Browserless 失败，将降级使用卡片或纯文本。"
                f" error={error}"
            )
            return False

    async def _capture(self, context, dyn_id: str, page_url: str) -> Optional[str]:
        page = await context.new_page()
        output_path = ""
        try:
            await page.goto(
                page_url, wait_until="domcontentloaded", timeout=self.timeout_ms
            )
            container = page.locator(OPUS_SELECTOR).first
            await container.wait_for(state="visible", timeout=self.timeout_ms)
            await page.evaluate(
                """selectors => {
                    selectors.forEach(selector => {
                        document.querySelectorAll(selector).forEach(
                            element => element.remove()
                        );
                    });
                    const style = document.createElement('style');
                    style.textContent = selectors.map(
                        selector => `${selector} { display: none !important; }`
                    ).join(' ');
                    document.head.appendChild(style);
                }""",
                list(OBSTRUCTIVE_PAGE_SELECTORS),
            )
            await page.evaluate("() => document.fonts.ready")
            await self._trigger_lazy_loading(page)
            try:
                await page.wait_for_function(
                    """selector => {
                        const root = document.querySelector(selector);
                        if (!root) return false;
                        return [...root.querySelectorAll('img')].every(
                            img => img.complete && img.naturalWidth > 0
                        );
                    }""",
                    arg=OPUS_SELECTOR,
                    timeout=self.timeout_ms,
                )
            except Exception:
                logger.warning(f"原生动态部分图片等待超时: dyn_id={dyn_id}")

            await self._wait_for_stable_layout(container)
            box = await container.bounding_box()
            if not box or box["width"] < 100 or box["height"] < 80:
                logger.warning(f"原生动态容器尺寸异常: dyn_id={dyn_id} box={box}")
                return None

            with tempfile.NamedTemporaryFile(
                prefix=f"bilibili_opus_{dyn_id}_", suffix=".jpg", delete=False
            ) as output_file:
                output_path = output_file.name
            await container.screenshot(
                path=output_path,
                type="jpeg",
                quality=95,
                animations="disabled",
                timeout=self.timeout_ms,
            )
            if os.path.exists(output_path) and os.path.getsize(output_path) > 4096:
                self._output_paths.add(output_path)
                return output_path
            return None
        finally:
            await page.close()
            if output_path and (
                not os.path.exists(output_path) or os.path.getsize(output_path) <= 4096
            ):
                try:
                    os.remove(output_path)
                except OSError:
                    pass

    @staticmethod
    async def _trigger_lazy_loading(page) -> None:
        """逐屏滚动动态主体，触发视口外图片的懒加载。"""
        await page.evaluate(
            """async selector => {
                const root = document.querySelector(selector);
                if (!root) return;
                const rect = root.getBoundingClientRect();
                const start = Math.max(0, rect.top + window.scrollY);
                const end = start + Math.max(rect.height, root.scrollHeight);
                const step = Math.max(400, Math.floor(window.innerHeight * 0.8));
                for (let y = start; y < end; y += step) {
                    window.scrollTo(0, y);
                    await new Promise(resolve => setTimeout(resolve, 250));
                }
                window.scrollTo(0, start);
                await new Promise(resolve => setTimeout(resolve, 500));
            }""",
            OPUS_SELECTOR,
        )

    @staticmethod
    async def _wait_for_stable_layout(container) -> None:
        previous = None
        stable_count = 0
        for _ in range(8):
            box = await container.bounding_box()
            current = (
                round(box["width"]),
                round(box["height"]),
            ) if box else None
            if current and current == previous:
                stable_count += 1
                if stable_count >= 2:
                    return
            else:
                stable_count = 0
            previous = current
            await asyncio.sleep(0.25)

    async def _stop_playwright_driver(self) -> None:
        self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    async def _reset_browser_objects(self) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        await self._stop_playwright_driver()
        self._credential_fingerprint = ()

    def release_output(self, path: Optional[str]) -> None:
        """删除由本渲染器生成且不再被消息缓存引用的截图。"""
        if not path or path not in self._output_paths:
            return
        self._output_paths.discard(path)
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning(f"清理原生动态截图失败: path={path} error={exc}")

    async def close(self) -> None:
        """关闭浏览器上下文和 Playwright 进程。"""
        async with self._lock:
            await self._reset_browser_objects()
            for path in list(self._output_paths):
                self.release_output(path)
