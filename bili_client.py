from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from astrbot.api import logger
from bilibili_api import Credential, opus, request_settings, user


class BiliClient:
    """
    负责所有与 Bilibili API 的交互。
    """

    def __init__(
        self,
        sessdata: Optional[str] = None,
        credential_dict: Optional[Dict[str, Any]] = None,
        proxy: Optional[str] = None,
    ) -> None:
        """
        初始化 Bilibili API 客户端。
        """
        self.proxy = (proxy or "").strip()
        self._apply_proxy()
        self.credential = None
        if credential_dict:
            self.credential = self._build_credential(credential_dict)
        elif sessdata:
            self.credential = self._build_credential({"sessdata": sessdata})
        else:
            logger.warning("未提供 SESSDATA 或 凭据，部分需要登录的API可能无法使用。")

    def _apply_proxy(self) -> None:
        """
        根据当前配置应用全局请求代理。
        """
        try:
            request_settings.set_proxy(self.proxy)
        except Exception as e:
            logger.warning(f"设置 Bilibili 请求代理失败: {e}")

    def _build_credential(self, credential_data: Dict[str, Any]) -> Credential:
        """
        构建 Credential，优先尝试携带 proxy 参数，失败时自动回退。
        """
        payload = dict(credential_data)
        if self.proxy:
            payload.setdefault("proxy", self.proxy)
        try:
            return Credential(**payload)
        except TypeError:
            payload.pop("proxy", None)
            return Credential(**payload)

    def set_credential(self, credential_dict: Dict[str, Any]) -> None:
        """
        设置凭据。
        """
        self.credential = self._build_credential(credential_dict)

    def get_credential_dict(self) -> Optional[Dict[str, Any]]:
        """
        获取当前凭据的字典形式。
        """
        if not self.credential:
            return None
        return {
            "sessdata": self.credential.sessdata,
            "bili_jct": self.credential.bili_jct,
            "buvid3": self.credential.buvid3,
            "buvid4": self.credential.buvid4,
            "dedeuserid": self.credential.dedeuserid,
            "ac_time_value": self.credential.ac_time_value,
        }

    async def check_credential(self) -> bool:
        """
        检查凭据是否有效。
        DEPRECATED: 该方法已废弃。
        """
        if not self.credential:
            return False
        return await self.credential.check_valid()

    async def refresh_credential(self) -> bool:
        """
        刷新凭据。
        DEPRECATED: 该方法已废弃。
        """
        if not self.credential:
            return False
        try:
            if await self.credential.check_refresh():
                await self.credential.refresh()
                return True
        except Exception as e:
            logger.error(f"刷新凭据失败: {e}")
        return False

    def start_refresh(
        self,
        on_refreshed: Optional[
            Callable[[Dict[str, Any] | None], Awaitable[None]]
        ] = None,
    ):
        """
        定时刷新凭据的循环。
        DEPRECATED: 该方法已废弃。
        :param on_refreshed: 兼容保留。过去用于刷新成功后的异步回调。
        """
        logger.warning(
            "start_refresh() 已废弃：为避免触发上游异常，已禁用定时刷新凭据任务。"
        )
        return

    def get_user(self, uid: int) -> user.User:
        """
        根据UID获取一个 User 对象。
        """
        return user.User(uid=uid, credential=self.credential)

    async def get_latest_dynamics(self, uid: int) -> Optional[Dict[str, Any]]:
        """
        获取用户的最新动态。
        """
        try:
            self._apply_proxy()
            u: user.User = self.get_user(uid)
            return await u.get_dynamics_new()
        except Exception as e:
            logger.error(f"获取用户动态失败 (UID: {uid}): {e}")
            return None

    async def get_opus_detail(self, opus_id: int) -> Optional[Dict[str, Any]]:
        """获取 Opus 完整详情，用于还原专栏的有序正文。"""
        try:
            self._apply_proxy()
            return await opus.Opus(
                opus_id=opus_id, credential=self.credential
            ).get_info()
        except Exception as e:
            logger.error(f"获取 Opus 详情失败 (ID: {opus_id}): {e}")
            return None

    async def get_user_info(self, uid: int) -> Tuple[Dict[str, Any] | None, str]:
        """
        获取用户的基本信息。
        """
        try:
            u: user.User = self.get_user(uid)
            info = await u.get_user_info()
            return info, ""
        except Exception as e:
            if "code" in e.args[0] and e.args[0]["code"] == -404:
                logger.warning(f"无法找到用户 (UID: {uid})")
                return None, "啥都木有 (´;ω;`)"
            else:
                logger.error(f"获取用户信息失败 (UID: {uid}): {e}")
                return None, f"获取 UP 主信息失败: {str(e)}"
