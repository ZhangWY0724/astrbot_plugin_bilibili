import importlib.util
import sys
import types
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock


class _Logger:
    def __init__(self):
        self.records = []

    def __getattr__(self, level):
        return lambda message, *args, **kwargs: self.records.append(
            (level, str(message))
        )

    def messages(self, level=None):
        return [message for name, message in self.records if level in (None, name)]


LOGGER = _Logger()


def _install_module(name, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class _Component:
    def __init__(self, *args, **kwargs):
        pass

    @classmethod
    def fromFileSystem(cls, _path):
        return cls()


class _Context:
    pass


@dataclass
class _RenderPayload:
    type: str = ""
    content_blocks: list = field(default_factory=list)


@dataclass
class _SubscriptionRecord:
    uid: int
    last: str = ""
    filter_types: list = None
    filter_regex: list = None
    recent_ids: list = None
    at_all: bool = False
    at_sub_users: list = None


@dataclass
class _DynamicParseResult:
    dyn_id: str = None
    payload: object = None
    skipped: bool = False
    reason: str = ""

    @classmethod
    def deliver(cls, payload, dyn_id):
        return cls(dyn_id=dyn_id, payload=payload)

    @classmethod
    def skip(cls, dyn_id, reason):
        return cls(dyn_id=dyn_id, skipped=True, reason=reason)

    def has_payload(self):
        return self.payload is not None


@dataclass
class _DispatchResult:
    sent: bool
    dropped: bool = False
    reason: str = ""


PACKAGE = "listener_logging_test_package"
_install_module(PACKAGE).__path__ = []
_install_module(f"{PACKAGE}.services").__path__ = []
_install_module(f"{PACKAGE}.core").__path__ = []

astrbot = _install_module("astrbot")
astrbot_api = _install_module("astrbot.api", logger=LOGGER)
astrbot.api = astrbot_api
_install_module(
    "astrbot.api.message_components",
    At=_Component,
    AtAll=_Component,
    File=_Component,
    Image=_Component,
    Plain=_Component,
)
_install_module("astrbot.core").__path__ = []
_install_module("astrbot.core.star", Context=_Context)

_install_module(f"{PACKAGE}.bili_client", BiliClient=object)
_install_module(
    f"{PACKAGE}.core.constant",
    resolve_render_mode=lambda *_args: "plain",
)
_install_module(f"{PACKAGE}.core.data_manager", DataManager=object)
_install_module(
    f"{PACKAGE}.core.models",
    DynamicParseResult=_DynamicParseResult,
    RenderPayload=_RenderPayload,
    SubscriptionRecord=_SubscriptionRecord,
)
_install_module(
    f"{PACKAGE}.core.utils",
    create_qrcode=lambda _value: "",
    image_to_base64=lambda _value: "",
    is_height_valid=lambda *_args: True,
    render_text_to_plain=lambda _value: "",
)
_install_module(
    f"{PACKAGE}.services.dispatcher",
    DispatchResult=_DispatchResult,
    SubscriptionNotification=object,
    SubscriptionNotificationDispatcher=object,
)
_install_module(f"{PACKAGE}.services.renderer", Renderer=object)

MODULE_PATH = Path(__file__).resolve().parents[1] / "services" / "listener.py"
SPEC = importlib.util.spec_from_file_location(
    f"{PACKAGE}.services.listener", MODULE_PATH
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class ListenerLoggingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        LOGGER.records.clear()
        self.listener = MODULE.DynamicListener.__new__(MODULE.DynamicListener)
        self.listener.dynamic_limit = 5
        self.listener.data_manager = types.SimpleNamespace(
            update_last_dynamic_id=AsyncMock()
        )
        self.listener.bili_client = types.SimpleNamespace(
            get_latest_dynamics=AsyncMock(return_value={"items": [{}, {}]}),
            get_opus_detail=AsyncMock(return_value=None),
        )
        self.listener.article_content_cache = MODULE.OrderedDict()
        self.listener.render_cache_limit = 32

    async def test_uid_task_logs_request_and_summary(self):
        self.listener._check_single_up = AsyncMock(return_value=(1, 1, 1))

        await self.listener._run_uid_task(
            123,
            [("aiocqhttp:GroupMessage:456", _SubscriptionRecord(uid=123))],
        )

        messages = LOGGER.messages("info")
        self.assertTrue(any("定时动态检测开始: uid=123 targets=1" in x for x in messages))
        self.assertTrue(any("item_count=2" in x for x in messages))
        self.assertTrue(
            any(
                "定时动态检测完成: uid=123 discovered=1 filtered=1 sent=1" in x
                for x in messages
            )
        )
        self.assertFalse(any("GroupMessage:456" in x for x in messages))

    async def test_new_and_filtered_dynamics_are_logged(self):
        payload = _RenderPayload(type="DYNAMIC_TYPE_DRAW")
        self.listener._parse_and_filter_dynamics = lambda *_args: [
            _DynamicParseResult.deliver(payload, "1001"),
            _DynamicParseResult.skip("1002", "lottery"),
        ]
        self.listener._handle_new_dynamic = AsyncMock(return_value=True)

        counts = await self.listener._check_single_up(
            "aiocqhttp:GroupMessage:456",
            _SubscriptionRecord(uid=123),
            dyn={"items": [{}, {}]},
            shared_payload=True,
        )

        self.assertEqual(counts, (1, 1, 1))
        messages = LOGGER.messages("info")
        self.assertTrue(
            any(
                "发现新动态: uid=123 dyn_id=1001 type=DYNAMIC_TYPE_DRAW" in x
                for x in messages
            )
        )
        self.assertTrue(
            any(
                "动态已过滤: uid=123 dyn_id=1002 reason=lottery" in x
                for x in messages
            )
        )

    async def test_request_exception_is_logged_and_task_completes(self):
        self.listener.bili_client.get_latest_dynamics = AsyncMock(
            side_effect=RuntimeError("request failed")
        )
        self.listener._check_single_up = AsyncMock(return_value=(0, 0, 0))

        await self.listener._run_uid_task(
            123,
            [("aiocqhttp:GroupMessage:456", _SubscriptionRecord(uid=123))],
        )

        self.assertTrue(
            any("拉取 UID=123 动态失败" in x for x in LOGGER.messages("error"))
        )
        messages = LOGGER.messages("info")
        self.assertTrue(any("uid=123 status=empty" in x for x in messages))
        self.assertTrue(any("定时动态检测完成: uid=123" in x for x in messages))

    async def test_no_new_dynamic_does_not_log_discovery(self):
        self.listener._parse_and_filter_dynamics = lambda *_args: []
        self.listener._handle_new_dynamic = AsyncMock(return_value=True)

        counts = await self.listener._check_single_up(
            "aiocqhttp:GroupMessage:456",
            _SubscriptionRecord(uid=123),
            dyn={"items": []},
            shared_payload=True,
        )

        self.assertEqual(counts, (0, 0, 0))
        self.assertFalse(any("发现新动态" in x for x in LOGGER.messages()))

    async def test_failed_dispatch_is_not_counted_as_sent(self):
        self.listener._parse_and_filter_dynamics = lambda *_args: [
            _DynamicParseResult.deliver(_RenderPayload(), "1001")
        ]
        self.listener._handle_new_dynamic = AsyncMock(return_value=False)

        counts = await self.listener._check_single_up(
            "aiocqhttp:GroupMessage:456",
            _SubscriptionRecord(uid=123),
            dyn={"items": [{}]},
            shared_payload=True,
        )

        self.assertEqual(counts, (1, 0, 0))

    async def test_article_detail_is_parsed_once_and_cached(self):
        block = types.SimpleNamespace(kind="images", image_urls=["image.jpg"])
        self.listener.bili_client.get_opus_detail = AsyncMock(
            return_value={"item": {"modules": []}}
        )

        def enrich(payload, _detail):
            payload.content_blocks = [block]
            return payload

        self.listener.renderer = types.SimpleNamespace(
            enrich_article_payload=enrich
        )
        first = _RenderPayload(type="DYNAMIC_TYPE_ARTICLE")
        second = _RenderPayload(type="DYNAMIC_TYPE_ARTICLE")

        await self.listener._enrich_article_payload(first, "123")
        await self.listener._enrich_article_payload(second, "123")

        self.assertEqual(first.content_blocks, [block])
        self.assertEqual(second.content_blocks, [block])
        self.listener.bili_client.get_opus_detail.assert_awaited_once_with(123)
        self.assertTrue(any("blocks=1 images=1" in x for x in LOGGER.messages()))

    async def test_article_detail_failure_keeps_summary_payload(self):
        first = _RenderPayload(type="DYNAMIC_TYPE_ARTICLE")
        second = _RenderPayload(type="DYNAMIC_TYPE_ARTICLE")
        self.listener.renderer = types.SimpleNamespace()

        await self.listener._enrich_article_payload(first, "456")
        await self.listener._enrich_article_payload(second, "456")

        self.assertEqual(first.content_blocks, [])
        self.assertEqual(second.content_blocks, [])
        self.listener.bili_client.get_opus_detail.assert_awaited_once_with(456)
        self.assertTrue(
            any("使用摘要卡片: dyn_id=456" in x for x in LOGGER.messages("warning"))
        )


if __name__ == "__main__":
    unittest.main()
