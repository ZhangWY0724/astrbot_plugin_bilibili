from __future__ import annotations

from dataclasses import dataclass, field
from inspect import isawaitable
import time
from typing import Any, Awaitable, Callable, Optional

from astrbot.api import logger
from astrbot.api.event import MessageEventResult
from astrbot.api.message_components import Node

SentHook = Callable[["SubscriptionNotification"], None | Awaitable[None]]


@dataclass(frozen=True)
class SubscriptionNotification:
    sub_user: str
    chain_parts: list[Any]
    send_node: bool = False
    dyn_id: Optional[str] = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DispatchResult:
    sent: bool
    dropped: bool = False
    reason: str = ""


class SubscriptionNotificationDispatcher:
    def __init__(
        self,
        context: Any,
        on_sent: Optional[SentHook] = None,
    ):
        self.context = context
        self.on_sent = on_sent
        self.silent_until_ts = 0

    async def publish(self, notification: SubscriptionNotification) -> DispatchResult:
        if self._is_silent(notification):
            return DispatchResult(sent=False, dropped=True, reason="silent_mode")

        result = self._build_event_result(notification)
        try:
            await self.context.send_message(notification.sub_user, result)
        except Exception as e:
            logger.error(
                f"发送订阅通知失败: sub_user={notification.sub_user} "
                f"dyn_id={notification.dyn_id} "
                f"error={e}"
            )
            return DispatchResult(sent=False, reason=str(e))
        await self._on_sent(notification)
        return DispatchResult(sent=True)

    def set_silent_until_ts(self, silent_until_ts: int) -> None:
        self.silent_until_ts = max(int(silent_until_ts), 0)

    async def _on_sent(self, notification: SubscriptionNotification) -> None:
        hook = self.on_sent
        if hook is None:
            return
        result = hook(notification)
        if isawaitable(result):
            await result

    def _is_silent(self, notification: SubscriptionNotification) -> bool:
        now_ts = max(int(time.time()), 0)
        if now_ts >= self.silent_until_ts:
            return False
        logger.info(
            f"订阅通知被静默丢弃: sub_user={notification.sub_user} dyn_id={notification.dyn_id}"
        )
        return True

    @staticmethod
    def _build_event_result(
        notification: SubscriptionNotification,
    ) -> MessageEventResult:
        if notification.send_node:
            qq_node = Node(
                uin=0,
                name="AstrBot",
                content=notification.chain_parts,
            )
            return MessageEventResult(chain=[qq_node])
        return MessageEventResult(chain=notification.chain_parts).use_t2i(False)
