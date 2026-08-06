import asyncio
import re
import time
import traceback
from collections import OrderedDict, defaultdict
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger
from astrbot.api.message_components import At, AtAll, File, Image, Plain
from astrbot.core.star import Context

from ..bili_client import BiliClient
from ..core.data_manager import DataManager
from ..core.constant import resolve_render_mode
from ..core.models import DynamicParseResult, RenderPayload, SubscriptionRecord
from ..core.utils import (
    create_qrcode,
    image_to_base64,
    is_height_valid,
    render_text_to_plain,
)
from .dispatcher import (
    DispatchResult,
    SubscriptionNotification,
    SubscriptionNotificationDispatcher,
)
from .renderer import Renderer

PLAIN_PUSH_ACTIONS = {
    "DYNAMIC_TYPE_AV": "投稿了新视频",
    "DYNAMIC_TYPE_ARTICLE": "发布了新专栏动态",
    "DYNAMIC_TYPE_DRAW": "发布了新图文动态",
    "DYNAMIC_TYPE_FORWARD": "转发了新动态",
    "DYNAMIC_TYPE_WORD": "发布了新动态",
}
VIDEO_BODY_PREFIX = "投稿了新视频"
GROUP_MESSAGE_TYPE = "GroupMessage"
MIN_AT_ALL_REMAINING = 1


class DynamicListener:
    """
    负责后台轮询检查 Bilibili 动态并推送更新。
    """

    def __init__(
        self,
        context: Context,
        data_manager: DataManager,
        bili_client: BiliClient,
        renderer: Renderer,
        dispatcher: SubscriptionNotificationDispatcher,
        cfg: dict,
    ):
        self.context = context
        self.data_manager = data_manager
        self.bili_client = bili_client
        self.renderer = renderer
        self.dispatcher = dispatcher
        self.interval_secs = max(1, int(cfg.get("interval_secs", 300)))
        self.task_gap_secs = self._parse_float(cfg.get("task_gap_secs"), 20, minimum=0)
        self.render_mode = resolve_render_mode(
            cfg.get("render_mode"), cfg.get("rai", True)
        )
        self.node = cfg.get("node", False)
        self.dynamic_limit = cfg.get("dynamic_limit", 5)
        self.render_cache: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self.render_cache_limit = int(cfg.get("render_cache_limit", 32))
        self.article_content_cache: OrderedDict[str, list] = OrderedDict()
        self.plain_push_template = (cfg.get("plain_push_template", "") or "").strip()
        self.plain_push_forward_template = (
            cfg.get("plain_push_forward_template", "") or ""
        ).strip()

    async def start(self):
        """启动后台监听循环（按 UID 任务池调度）。"""
        uid_states: Dict[int, float] = {}
        next_dispatch_at = 0.0

        while True:
            try:
                if self.bili_client.credential is None:
                    logger.warning(
                        "Bilibili 凭据未设置，无法获取动态。请使用 /bili_login 登录或在配置中设置 sessdata。"
                    )
                    await asyncio.sleep(self.interval_secs)
                    continue

                uid_targets = self._build_uid_targets()
                current_uids = set(uid_targets.keys())
                now = time.monotonic()

                for uid in list(uid_states):
                    if uid not in current_uids:
                        uid_states.pop(uid, None)

                for uid in current_uids:
                    uid_states.setdefault(uid, now)

                if not current_uids:
                    await asyncio.sleep(2)
                    continue

                due_uids = [uid for uid in current_uids if uid_states[uid] <= now]
                if not due_uids:
                    next_due_at = min(uid_states[uid] for uid in current_uids)
                    wait_secs = min(max(next_due_at - now, 0.2), 2.0)
                    await asyncio.sleep(wait_secs)
                    continue

                if now < next_dispatch_at:
                    wait_secs = min(max(next_dispatch_at - now, 0.2), 2.0)
                    await asyncio.sleep(wait_secs)
                    continue

                run_uid = min(due_uids, key=lambda uid: (uid_states[uid], uid))
                await self._run_uid_task(run_uid, uid_targets.get(run_uid, []))

                finished_at = time.monotonic()
                uid_states[run_uid] = finished_at + self.interval_secs
                next_dispatch_at = finished_at + self.task_gap_secs
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"UID任务池调度异常: {e}\n{traceback.format_exc()}")
                await asyncio.sleep(1)

    @staticmethod
    def _parse_float(value: Any, default: float, minimum: float = 0) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        return max(parsed, minimum)

    def _build_uid_targets(self) -> Dict[int, List[Tuple[str, SubscriptionRecord]]]:
        """构建 UID -> 订阅目标列表 的映射，用于 UID 级去重请求。"""
        uid_targets: Dict[int, List[Tuple[str, SubscriptionRecord]]] = {}
        all_subs = self.data_manager.get_all_subscriptions()

        for sub_user, sub_list in all_subs.items():
            for sub_data in sub_list or []:
                uid = sub_data.uid
                try:
                    uid_int = int(uid)
                except (TypeError, ValueError):
                    continue

                uid_targets.setdefault(uid_int, []).append((sub_user, sub_data))

        return uid_targets

    async def _run_uid_task(
        self, uid: int, targets: List[Tuple[str, SubscriptionRecord]]
    ) -> None:
        """执行单个 UID 的任务：动态仅请求一次，再按订阅分发。"""
        if not targets:
            return

        started_at = time.monotonic()
        logger.info(f"定时动态检测开始: uid={uid} targets={len(targets)}")
        request_started_at = time.monotonic()
        try:
            dyn = await self.bili_client.get_latest_dynamics(uid)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"拉取 UID={uid} 动态失败: {e}\n{traceback.format_exc()}")
            dyn = None

        items = dyn.get("items") if isinstance(dyn, dict) else None
        item_count = len(items) if isinstance(items, list) else 0
        request_elapsed_ms = int((time.monotonic() - request_started_at) * 1000)
        logger.info(
            f"动态接口请求完成: uid={uid} status={'ok' if dyn else 'empty'} "
            f"item_count={item_count} elapsed_ms={request_elapsed_ms}"
        )

        discovered = 0
        filtered = 0
        sent = 0
        for sub_user, sub_data in targets:
            try:
                target_discovered, target_filtered, target_sent = (
                    await self._check_single_up(
                        sub_user=sub_user,
                        sub_data=sub_data,
                        dyn=dyn,
                        shared_payload=True,
                    )
                )
                discovered += target_discovered
                filtered += target_filtered
                sent += target_sent
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    f"处理订阅目标失败: uid={sub_data.uid} "
                    f"platform={self._resolve_platform_name(sub_user) or 'unknown'} "
                    f"error={e}\n{traceback.format_exc()}"
                )

        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        logger.info(
            f"定时动态检测完成: uid={uid} discovered={discovered} "
            f"filtered={filtered} sent={sent} elapsed_ms={elapsed_ms}"
        )

    async def _check_single_up(
        self,
        sub_user: str,
        sub_data: SubscriptionRecord,
        dyn: Optional[Dict[str, Any]] = None,
        shared_payload: bool = False,
    ) -> Tuple[int, int, int]:
        """检查单个订阅的UP主是否有更新。"""
        uid = int(sub_data.uid)
        discovered = 0
        filtered = 0
        sent = 0

        # 检查动态更新
        if dyn is None and not shared_payload:
            dyn = await self.bili_client.get_latest_dynamics(uid)
        if dyn:
            result_list = self._parse_and_filter_dynamics(dyn, sub_data)
            attempted = 0
            for result in reversed(result_list):
                if result.has_payload():
                    discovered += 1
                    logger.info(
                        f"发现新动态: uid={uid} dyn_id={result.dyn_id} "
                        f"type={result.payload.type or 'unknown'}"
                    )
                    if attempted < self.dynamic_limit:
                        attempted += 1
                        if await self._handle_new_dynamic(
                            sub_user, result.payload, result.dyn_id, sub_data
                        ):
                            sent += 1
                    if result.dyn_id:
                        await self.data_manager.update_last_dynamic_id(
                            sub_user, uid, result.dyn_id
                        )
                elif result.dyn_id:
                    filtered += 1
                    logger.info(
                        f"动态已过滤: uid={uid} dyn_id={result.dyn_id} "
                        f"reason={result.reason or 'unknown'}"
                    )
                    await self.data_manager.update_last_dynamic_id(
                        sub_user, uid, result.dyn_id
                    )
        return discovered, filtered, sent

    async def _enrich_article_payload(
        self, payload: RenderPayload, dyn_id: Optional[str]
    ) -> None:
        """在专栏发送前补全有序正文，失败时保留列表摘要。"""
        if not dyn_id:
            return

        if dyn_id in self.article_content_cache:
            cached = self.article_content_cache[dyn_id]
            self.article_content_cache.move_to_end(dyn_id)
            payload.content_blocks = list(cached)
            return

        detail = await self.bili_client.get_opus_detail(int(dyn_id))
        if not detail:
            self._cache_article_content(dyn_id, [])
            logger.warning(f"专栏详情获取失败，使用摘要卡片: dyn_id={dyn_id}")
            return

        self.renderer.enrich_article_payload(payload, detail)
        if not payload.content_blocks:
            self._cache_article_content(dyn_id, [])
            logger.warning(f"专栏详情没有可渲染段落，使用摘要卡片: dyn_id={dyn_id}")
            return

        self._cache_article_content(dyn_id, payload.content_blocks)
        image_count = sum(
            len(block.image_urls)
            for block in payload.content_blocks
            if block.kind == "images"
        )
        logger.info(
            f"专栏详情已补全: dyn_id={dyn_id} "
            f"blocks={len(payload.content_blocks)} images={image_count}"
        )

    def _cache_article_content(self, dyn_id: str, blocks: list) -> None:
        self.article_content_cache[dyn_id] = list(blocks)
        self.article_content_cache.move_to_end(dyn_id)
        while len(self.article_content_cache) > self.render_cache_limit:
            self.article_content_cache.popitem(last=False)

    def _build_plain_header(self, payload: Any, nested: bool) -> str:
        render_type = payload.type
        name = payload.name
        if not isinstance(name, str):
            name = ""
        if not isinstance(render_type, str):
            render_type = ""
        display_name = name.strip() or "未知作者"

        action = PLAIN_PUSH_ACTIONS.get(render_type, "发布了新动态")

        subject = "原动态作者" if nested else "UP 主"
        return f"📣 {subject} 「{display_name}」 {action}:"

    def _build_plain_body(self, payload: Any) -> str:
        summary = (payload.summary or "").strip()
        if summary:
            return summary
        plain_text = render_text_to_plain(payload.text)
        if payload.type == "DYNAMIC_TYPE_AV" and plain_text.startswith(
            VIDEO_BODY_PREFIX
        ):
            return plain_text.removeprefix(VIDEO_BODY_PREFIX).strip()
        return plain_text

    def _compose_plain_push(
        self,
        payload: Any,
        render_fail: bool = False,
        nested: bool = False,
    ) -> list:
        """转换为非图片模式下的消息链。"""
        chain = []
        if render_fail and not nested:
            chain.append(Plain("渲染图片失败了 (´;ω;`)\n"))

        header_item = self._build_plain_header(payload, nested)
        title_item = f"标题: {payload.title}" if payload.title else ""
        body_raw = self._build_plain_body(payload)

        lines = list(filter(None, [header_item, title_item, body_raw]))
        if lines:
            chain.append(Plain("\n".join(lines)))

        for pic in filter(None, payload.image_urls):
            chain.append(Image.fromURL(pic))

        # 转发类型的转发部分会进入此分支
        # [TODO] 此处"转发内容:"后的换行需要实现
        forward_data = getattr(payload, "forward", None)
        if forward_data:
            chain.append(Plain("\u200b\n转发内容:"))
            chain.extend(self._compose_plain_push(forward_data, nested=True))

        url = payload.url
        if url and not nested:
            chain.append(Plain(f"\n{url}"))
        return chain

    def _compose_template_push(self, payload: Any, render_fail: bool = False) -> list:
        """使用自定义模板构建非图片模式下的消息链。"""
        chain: list = []
        if render_fail:
            chain.append(Plain("渲染图片失败了 (´;ω;`)\n"))

        text = self._format_payload_template(self.plain_push_template, payload)
        if text is None:
            return self._compose_plain_push(payload, render_fail=render_fail)
        if text:
            chain.append(Plain(text))

        for pic in filter(None, payload.image_urls):
            chain.append(Image.fromURL(pic))

        forward_data = getattr(payload, "forward", None)
        if forward_data:
            if self.plain_push_forward_template:
                fwd_text = self._format_payload_template(
                    self.plain_push_forward_template,
                    forward_data,
                    with_action=False,
                )
                if fwd_text is None:
                    chain.extend(self._compose_plain_push(forward_data, nested=True))
                else:
                    chain.append(Plain("\u200b\n转发内容:"))
                    if fwd_text:
                        chain.append(Plain(f"\n{fwd_text}"))
                    for pic in filter(None, forward_data.image_urls):
                        chain.append(Image.fromURL(pic))
            else:
                chain.append(Plain("\u200b\n转发内容:"))
                chain.extend(self._compose_plain_push(forward_data, nested=True))

        return chain

    def _format_payload_template(
        self, template: str, payload: Any, *, with_action: bool = True
    ) -> Optional[str]:
        """用 payload 字段格式化模板，返回去除空行后的文本。格式化失败返回 None。"""
        name = payload.name.strip() if isinstance(payload.name, str) else ""
        render_type = payload.type if isinstance(payload.type, str) else ""
        ctx: Dict[str, str] = {
            "name": name or "未知作者",
            "uid": str(getattr(payload, "uid", "") or ""),
            "title": str(payload.title or ""),
            "text": self._build_plain_body(payload),
            "url": str(payload.url or ""),
        }
        if with_action:
            ctx["action"] = PLAIN_PUSH_ACTIONS.get(render_type, "发布了新动态")

        try:
            formatted = template.format_map(defaultdict(str, ctx))
        except Exception as e:
            logger.warning(f"消息模板格式化失败: {e}，回退到默认格式")
            return None

        lines = [line for line in formatted.split("\n") if line.strip()]
        return "\n".join(lines)

    async def _send_dynamic(
        self,
        sub_user: str,
        chain_parts: list,
        send_node: bool = False,
        dyn_id: Optional[str] = None,
    ) -> DispatchResult:
        notification = SubscriptionNotification(
            sub_user=sub_user,
            chain_parts=chain_parts,
            send_node=self.node or send_node,
            dyn_id=dyn_id,
        )
        return await self.dispatcher.publish(notification)

    def _cache_render(
        self,
        dyn_id: Optional[str],
        chain_parts: list,
        send_node: bool,
    ):
        """缓存渲染结果，避免同一动态在不同会话重复渲染。"""
        if not dyn_id:
            return
        self.render_cache.pop(dyn_id, None)
        self.render_cache[dyn_id] = {
            "chain": chain_parts,
            "send_node": send_node,
        }
        while len(self.render_cache) > self.render_cache_limit:
            self.render_cache.popitem(last=False)

    async def _handle_new_dynamic(
        self,
        sub_user: str,
        payload: Optional[RenderPayload],
        dyn_id: Optional[str] = None,
        sub_data: Optional[SubscriptionRecord] = None,
        use_cache: bool = True,
    ) -> bool:
        """处理并发送新的动态通知。"""
        if not payload:
            return False

        permit_atall = await self._check_atall_permission(
            sub_user, bool(sub_data and sub_data.at_all)
        )

        cached = self.render_cache.get(dyn_id) if dyn_id and use_cache else None
        if cached:
            logger.debug(
                f"动态推送命中缓存: dyn_id={dyn_id} "
                f"platform={self._resolve_platform_name(sub_user) or 'unknown'}"
            )
            chain_to_send = self._add_at_components(
                list(cached["chain"]), sub_data, permit_atall=permit_atall
            )
            try:
                dispatch_result = await self._send_dynamic(
                    sub_user,
                    chain_to_send,
                    send_node=cached["send_node"],
                    dyn_id=dyn_id,
                )
                if dispatch_result.sent:
                    logger.info(
                        "动态推送完成(缓存): "
                        f"platform={self._resolve_platform_name(sub_user) or 'unknown'} "
                        f"dyn_id={dyn_id}"
                    )
                else:
                    logger.warning(
                        "动态推送未发送(缓存): "
                        f"platform={self._resolve_platform_name(sub_user) or 'unknown'} "
                        f"dyn_id={dyn_id} "
                        f"reason={dispatch_result.reason or 'unknown'}"
                    )
                return dispatch_result.sent
            except Exception as e:
                logger.error(
                    "发送缓存动态失败（已忽略）: "
                    f"platform={self._resolve_platform_name(sub_user) or 'unknown'} "
                    f"dyn_id={dyn_id} error={e}"
                )
                return False

        send_node_flag = self.node
        ls, actual_mode = await self._build_render_chain(sub_user, payload, dyn_id)
        if not ls:
            return False
        if use_cache:
            self._cache_render(dyn_id, ls, send_node_flag)
        chain_to_send = self._add_at_components(
            list(ls), sub_data, permit_atall=permit_atall
        )
        try:
            dispatch_result = await self._send_dynamic(
                sub_user,
                chain_to_send,
                send_node=send_node_flag,
                dyn_id=dyn_id,
            )
            if dispatch_result.sent:
                logger.info(
                    f"动态推送完成({actual_mode}): "
                    f"platform={self._resolve_platform_name(sub_user) or 'unknown'} "
                    f"dyn_id={dyn_id}"
                )
            else:
                logger.warning(
                    f"动态推送未发送({actual_mode}): "
                    f"platform={self._resolve_platform_name(sub_user) or 'unknown'} "
                    f"dyn_id={dyn_id} reason={dispatch_result.reason or 'unknown'}"
                )
            return dispatch_result.sent
        except Exception as e:
            logger.error(
                f"动态推送失败（已忽略）: "
                f"platform={self._resolve_platform_name(sub_user) or 'unknown'} "
                f"dyn_id={dyn_id} error={e}"
            )
            return False
    async def _build_render_chain(
        self, sub_user: str, payload: RenderPayload, dyn_id: Optional[str]
    ) -> Tuple[list, str]:
        """按配置构建消息链，卡片失败时降级为纯文本。"""
        if self.render_mode == "plain":
            return self._build_plain_chain(payload), "纯文本"

        if payload.type == "DYNAMIC_TYPE_ARTICLE":
            await self._enrich_article_payload(payload, dyn_id)

        img_path = await self.renderer.render_dynamic(payload)
        if img_path:
            return (
                self._build_image_chain(sub_user, img_path, payload.url),
                "卡片图片",
            )

        logger.warning(f"卡片渲染失败，降级纯文本: dyn_id={dyn_id}")
        return (
            self._build_plain_chain(payload, render_fail=True),
            "降级纯文本",
        )

    def _build_plain_chain(
        self, payload: RenderPayload, render_fail: bool = False
    ) -> list:
        if self.plain_push_template:
            return self._compose_template_push(payload, render_fail=render_fail)
        return self._compose_plain_push(payload, render_fail=render_fail)

    def _build_image_chain(self, sub_user: str, img_path: str, url: str) -> list:
        platform_name = self._resolve_platform_name(sub_user)
        if is_height_valid(img_path, platform_name):
            chain = [Image.fromFileSystem(img_path)]
        else:
            timestamp = int(time.time())
            filename = f"bilibili_dynamic_{timestamp}.jpg"
            chain = [File(file=img_path, name=filename)]
        if url:
            chain.append(Plain(f"\n{url}"))
        return chain

    def _resolve_platform_name(self, sub_user: str) -> str:
        """解析 sub_user 所属平台的类型名（如 telegram、aiocqhttp）。"""
        adapter_id = sub_user.split(":", 1)[0] if ":" in sub_user else ""
        if not adapter_id:
            return ""
        platform_inst = self.context.get_platform_inst(adapter_id)
        if platform_inst:
            return platform_inst.meta().name
        return ""

    @staticmethod
    def _extract_group_session(sub_user: str) -> Optional[Tuple[str, str]]:
        try:
            platform_id, message_type, session_id = sub_user.split(":", 2)
        except ValueError:
            return None
        if message_type != GROUP_MESSAGE_TYPE:
            return None
        group_id = session_id.split("_")[-1].strip()
        if not group_id:
            return None
        return platform_id, group_id

    @staticmethod
    def _extract_action_data(action_result: Any) -> Dict[str, Any]:
        if not isinstance(action_result, dict):
            return {}
        payload = action_result.get("data")
        if isinstance(payload, dict):
            return payload
        return action_result

    @staticmethod
    def _prepend_atall(chain_parts: List[Any]) -> List[Any]:
        return [AtAll(), Plain(" ")] + chain_parts

    @staticmethod
    def _add_at_components(
        chain_parts: List[Any],
        sub_data: Optional[SubscriptionRecord],
        permit_atall: bool = True,
    ) -> List[Any]:
        if not sub_data:
            return chain_parts
        at_components = []
        should_at_all = sub_data.at_all and permit_atall
        if should_at_all:
            at_components.extend([AtAll(), Plain(" ")])
        elif sub_data.at_sub_users:
            for uid in sub_data.at_sub_users:
                at_components.extend([At(qq=uid), Plain(" ")])
        return at_components + chain_parts

    async def _check_atall_permission(self, sub_user: str, enabled: bool) -> bool:
        if not enabled:
            return False

        group_ctx = self._extract_group_session(sub_user)
        if not group_ctx:
            logger.info(f"at_all 仅支持群聊会话，当前会话: {sub_user}")
            return False

        platform_id, group_id = group_ctx
        platform_inst = self.context.get_platform_inst(platform_id)
        if not platform_inst:
            logger.warning(f"at_all 失败：找不到平台实例 {platform_id}")
            return False

        client = platform_inst.get_client()
        if not client or not hasattr(client, "call_action"):
            logger.warning(f"at_all 失败：平台 {platform_id} 不支持 call_action")
            return False

        group_id_param: int | str = int(group_id) if group_id.isdigit() else group_id

        # 尝试通过 get_group_member_info 严格检查机器人自身权限（针对 napcat/lagrange 等 get_group_at_all_remain 不可靠的实现）
        try:
            bot_info_raw = await client.call_action("get_login_info")
            bot_info = self._extract_action_data(bot_info_raw)
            bot_id = bot_info.get("user_id")
            if bot_id:
                member_info_raw = await client.call_action(
                    "get_group_member_info", group_id=group_id_param, user_id=bot_id
                )
                member_info = self._extract_action_data(member_info_raw)
                role = member_info.get("role")
                if role and role not in ["admin", "owner"]:
                    logger.info(
                        f"机器人(UID:{bot_id})在群 {group_id} 角色为 {role}，无 @全体成员 权限"
                    )
                    return False
        except Exception as e:
            logger.debug(
                f"通过 get_group_member_info 检查权限失败，降级使用 get_group_at_all_remain: {e}"
            )

        try:
            remain_raw = await client.call_action(
                "get_group_at_all_remain", group_id=group_id_param
            )
        except Exception as e:
            logger.warning(f"调用 get_group_at_all_remain 失败: {e}")
            return False

        remain_data = self._extract_action_data(remain_raw)
        can_at_all = bool(remain_data.get("can_at_all"))
        group_remain = int(remain_data.get("remain_at_all_count_for_group", 0) or 0)
        self_remain_value = remain_data.get(
            "remain_at_all_count_for_self",
            remain_data.get("remain_at_all_count_for_uin", 0),
        )
        self_remain = int(self_remain_value or 0)

        if not can_at_all:
            logger.info(f"群 {group_id} 当前不允许 @全体成员")
            return False
        if group_remain < MIN_AT_ALL_REMAINING or self_remain < MIN_AT_ALL_REMAINING:
            logger.info(
                f"群 {group_id} @全体次数不足: group={group_remain}, self={self_remain}"
            )
            return False
        return True

    def _get_dynamic_items(self, dyn: Dict[str, Any], data: SubscriptionRecord):
        """获取动态条目列表。"""
        last = data.last
        items = dyn["items"]
        recent_ids = data.recent_ids
        known_ids = {x for x in ([last] + recent_ids) if x}
        new_items = []

        for item in items:
            if "modules" not in item:
                continue
            # 过滤置顶
            if (
                item["modules"].get("module_tag")
                and item["modules"]["module_tag"].get("text") == "置顶"
            ):
                continue

            if item["id_str"] in known_ids:
                break
            new_items.append(item)

        return new_items

    def _match_filter_regex(
        self, text: Optional[str], filter_regex: List[str], log_template: str
    ) -> bool:
        """检测文本是否命中过滤正则"""
        if not text or not filter_regex:
            return False

        for regex_pattern in filter_regex:
            try:
                if re.search(regex_pattern, text):
                    logger.info(log_template.format(regex_pattern=regex_pattern))
                    return True
            except re.error:
                logger.warning(f"无效的正则表达式: {regex_pattern}")
                continue

        return False

    def _parse_and_filter_dynamics(
        self, dyn: Dict[str, Any], data: SubscriptionRecord
    ) -> List[DynamicParseResult]:
        """
        解析并过滤动态。
        """
        filter_types = data.filter_types
        filter_regex = data.filter_regex
        uid = str(data.uid)
        items = self._get_dynamic_items(dyn, data)  # 不含last及置顶的动态列表
        result_list: List[DynamicParseResult] = []
        if not items:
            return result_list

        for item in items:
            dyn_id = item["id_str"]
            item_type = item.get("type")

            if item_type == "DYNAMIC_TYPE_FORWARD":
                result = self._handle_forward_dynamic(
                    item, dyn_id, uid, filter_types, filter_regex
                )
            elif item_type in ("DYNAMIC_TYPE_DRAW", "DYNAMIC_TYPE_WORD"):
                result = self._handle_draw_or_word_dynamic(
                    item, dyn_id, uid, filter_types, filter_regex
                )
            elif item_type == "DYNAMIC_TYPE_AV":
                result = self._handle_video_dynamic(item, dyn_id, uid, filter_types)
            elif item_type == "DYNAMIC_TYPE_ARTICLE":
                result = self._handle_article_dynamic(item, dyn_id, uid, filter_types)
            else:
                # dyn_id记为None，避免未识别类型挤占正常动态缓存
                result = DynamicParseResult.skip(None, "unsupported type")

            result_list.append(result)

        return result_list

    def _handle_forward_dynamic(
        self,
        item: Dict,
        dyn_id: str,
        uid: str,
        filter_types: List[str],
        filter_regex: List[str],
    ) -> DynamicParseResult:
        """处理转发动态的过滤与渲染数据准备。"""
        try:
            is_forward_lottery = (
                item["orig"]["modules"]["module_dynamic"]["major"]["opus"]["summary"][
                    "rich_text_nodes"
                ][0].get("text")
                == "互动抽奖"
            )
        except (KeyError, TypeError):
            is_forward_lottery = False

        if "forward_lottery" in filter_types and is_forward_lottery:
            logger.info(f"转发互动抽奖在过滤列表 {filter_types} 中。")
            return DynamicParseResult.skip(dyn_id, "forward_lottery")

        if "forward" in filter_types:
            logger.info(f"转发类型在过滤列表 {filter_types} 中。")
            return DynamicParseResult.skip(dyn_id, "forward")

        try:
            content_text = item["modules"]["module_dynamic"]["desc"]["text"]
        except (TypeError, KeyError):
            content_text = ""

        if "lottery" in filter_types and re.search(
            r"恭喜.*等\d+位同学中奖，已私信通知，详情请点击抽奖查看。",
            content_text,
        ):
            logger.info(f"转发内容为抽奖在过滤列表 {filter_types} 中。")
            return DynamicParseResult.skip(dyn_id, "lottery")

        if self._match_filter_regex(
            content_text, filter_regex, "转发内容匹配正则 {regex_pattern}。"
        ):
            return DynamicParseResult.skip(dyn_id, "regex")

        render_data = self.renderer.build_render_data(item)
        render_data.uid = uid
        render_data.url = f"https://t.bilibili.com/{dyn_id}"
        render_data.qrcode = create_qrcode(render_data.url)

        render_forward = self.renderer.build_render_data(
            item.get("orig", {}), is_forward=True
        )
        render_data.forward = render_forward.to_forward_payload()
        return DynamicParseResult.deliver(render_data, dyn_id)

    def _handle_draw_or_word_dynamic(
        self,
        item: Dict,
        dyn_id: str,
        uid: str,
        filter_types: List[str],
        filter_regex: List[str],
    ) -> DynamicParseResult:
        """处理图文/文字动态。"""
        if "draw" in filter_types:
            logger.info(f"图文类型在过滤列表 {filter_types} 中。")
            return DynamicParseResult.skip(dyn_id, "draw")

        major = item.get("modules", {}).get("module_dynamic", {}).get("major", {})
        if major.get("type") == "MAJOR_TYPE_BLOCKED":
            logger.info(f"图文动态 {dyn_id} 为充电专属。")
            return DynamicParseResult.skip(dyn_id, "major_blocked")

        opus = major.get("opus", {})
        summary = opus.get("summary", {})
        summary_text = summary.get("text", "")
        rich_nodes = summary.get("rich_text_nodes", [])
        first_node_text = rich_nodes[0].get("text") if rich_nodes else ""

        if first_node_text == "互动抽奖" and "lottery" in filter_types:
            logger.info(f"互动抽奖在过滤列表 {filter_types} 中。")
            return DynamicParseResult.skip(dyn_id, "lottery")

        if self._match_filter_regex(
            summary_text,
            filter_regex,
            f"图文动态 {dyn_id} 的 summary 匹配正则 '{{regex_pattern}}'。",
        ):
            return DynamicParseResult.skip(dyn_id, "regex")

        render_data = self.renderer.build_render_data(item)
        render_data.uid = uid
        return DynamicParseResult.deliver(render_data, dyn_id)

    def _handle_video_dynamic(
        self, item: Dict, dyn_id: str, uid: str, filter_types: List[str]
    ) -> DynamicParseResult:
        """处理视频动态。"""
        if "video" in filter_types:
            logger.info(f"视频类型在过滤列表 {filter_types} 中。")
            return DynamicParseResult.skip(dyn_id, "video")

        render_data = self.renderer.build_render_data(item)
        render_data.uid = uid
        return DynamicParseResult.deliver(render_data, dyn_id)

    def _handle_article_dynamic(
        self, item: Dict, dyn_id: str, uid: str, filter_types: List[str]
    ) -> DynamicParseResult:
        """处理专栏文章动态。"""
        if "article" in filter_types:
            logger.info(f"文章类型在过滤列表 {filter_types} 中。")
            return DynamicParseResult.skip(dyn_id, "article")

        major = item.get("modules", {}).get("module_dynamic", {}).get("major", {})
        if major.get("type") == "MAJOR_TYPE_BLOCKED":
            logger.info(f"文章 {dyn_id} 为充电专属。")
            return DynamicParseResult.skip(dyn_id, "major_blocked")

        render_data = self.renderer.build_render_data(item)
        render_data.uid = uid
        return DynamicParseResult.deliver(render_data, dyn_id)
