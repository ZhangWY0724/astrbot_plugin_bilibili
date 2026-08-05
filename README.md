# astrbot_plugin_bilibili_aikaid

这是一个为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 设计的 Bilibili 订阅提醒插件。

## ✨ 功能特性

- **UP 主动态订阅**：支持视频、图文、文字、专栏和转发动态。
- **定时检测与去重**：按 UID 合并轮询，避免同一动态重复提醒。
- **灵活过滤**：支持按动态类型、关键词正则和互动抽奖规则过滤。
- **多种提醒方式**：支持纯文本、插件图片卡片和 Bilibili 原生动态截图，并提供自动降级。
- **登录与凭据持久化**：支持管理员扫码登录，并在插件数据目录保存登录凭据。

![image](https://github.com/user-attachments/assets/972b2b99-b801-45cf-a882-6d841c9e8137)
## 🚀 安装

- 在插件市场下载
- 通过以下指令进行安装：

```shell
plugin i https://github.com/ZhangWY0724/astrbot_plugin_bilibili
```

## ⚙️ 配置

插件需要有效的 Bilibili 登录凭据才能稳定获取订阅数据。可使用以下两种方式：
1. 参考 [此指南](https://nemo2011.github.io/bilibili-api/#/get-credential) 获取你的 `sessdata`。

<img width="1453" alt="image" src="https://github.com/user-attachments/assets/d5342767-8e5c-4222-81da-f1cdb4b30c95">

2. 使用`/bili_login`指令获取登录二维码，扫码登录后插件会自动获取并保存凭据。
此方式有利于解决[issue #58](https://github.com/Soulter/astrbot_plugin_bilibili/issues/58)所述412问题。不推荐使用主账号登录。

### 渲染方式

配置项 `render_mode` 控制动态提醒的内容形式：

- `auto`：兼容旧版 `rai` 配置；`rai=true` 使用插件卡片，`rai=false` 使用纯文本。
- `plain`：纯文本消息。
- `card`：使用 AstrBot HTML 模板生成图片卡片。
- `native`：使用插件自带的 Playwright 无头 Chromium 打开 `https://www.bilibili.com/opus/{动态ID}`，截取 Bilibili 页面中的 `.bili-opus-view` 容器。

`native` 模式失败时会自动降级为 `card`，卡片也失败时再降级为纯文本。原生模式首次使用可能需要下载 Chromium；可通过 `native_browser_install` 选择 `auto`、`manual` 或 `disable`。Python 依赖由插件目录下的 `requirements.txt` 按 AstrBot 规范安装，Chromium 浏览器运行时不打包进插件仓库。

启用 `native` 后，插件加载完成会在后台自动准备 Playwright Chromium；因此 Python 的 `playwright` 依赖安装成功后，会在插件启动阶段继续自动下载 Chromium，不必等到第一条订阅推送才开始。Linux 环境使用 `auto` 时还会自动补齐 Chromium 系统运行库，适配以 root 运行的 Debian/Ubuntu Docker 容器。下载或系统依赖安装失败不会阻止插件启动，并会按上述规则降级渲染。

原生渲染使用配置项 `proxy`，与 Bilibili API 请求共用代理；扫码登录和持久化凭据会转换为浏览器 Cookie 使用，不会新增登录体系。

原生页面默认使用 `1920×1080` 桌面视口和 `1` 倍设备像素比，再对 `.bili-opus-view` 动态主体执行元素截图；输出尺寸由动态主体实际高度决定，不会填充为固定的整屏画布。

> 当前原生截图处于联调阶段：截图前会移除 `.v-popover-content` 登录浮层，并临时固定渲染动态 `1232710769951375376`。订阅检测、去重和消息目标仍使用实际动态数据。


## 📖 使用说明

### 动态订阅指令

| 指令 | 参数 | 说明 | 别名 |
| :--- | :--- | :--- | :--- |
| **bili_sub** | `<B站UID> [过滤器...]` | 订阅指定 UP 主的动态。可以添加多个过滤器（以空格分隔）以排除不感兴趣的内容。 | `订阅动态` |
| **bili_sub_list** | (无) | 显示当前会话的所有订阅。 | `订阅列表` |
| **bili_sub_del** | `<B站UID>` | 删除当前会话中对指定 UP 主的订阅。 | `订阅删除` |
| **bili_global_del** | `<SID>` | **[管理员]** 删除指定会话的所有订阅。使用 `/sid` 指令可查看会话 UMO。建议以「」包裹SID，形如`/bili_global_del 「QQ 12345:FriendMessage:67890」` | `全局删除` |
| **bili_global_list** | (无) | **[管理员]** 查看所有会话的订阅情况。 | `全局列表` |
| **bili_global_sub** | `<SID> <B站UID> [过滤器...]` | **[管理员]** 为指定会话（UMO）添加对 UP 主的订阅。建议以「」包裹SID，形如`/bili_global_sub 「QQ 12345:FriendMessage:67890」 123456` | `全局订阅` |
| **bili_sub_test** | `<B站UID>` | 测试订阅功能。仅测试获取动态与渲染图片功能，不保存订阅信息。 | `订阅测试` |
| **bili_card_style** | `[样式名]` | **[管理员]** 切换动态卡片渲染样式。不带参数查看可用样式列表。 | `卡片样式` |
| **bili_login** | (无) | **[管理员]** 获取二维码以登录。仅支持在私聊中触发。 | (无) |
| **bili_logout** | (无) | **[管理员]** 删除已保存的登录凭据，转而采用配置项中的sessdata（如果有） | (无) |

#### 参数说明

**1. 过滤器（过滤不感兴趣的内容）**

  - `forward`：过滤掉转发动态。
  - `lottery`：过滤掉互动抽奖动态。
  - `video`：过滤掉视频发布动态。
  - `article`：过滤掉专栏动态。
  - `draw`：过滤掉图文动态。
  - `forward_lottery`：过滤掉转发的互动抽奖动态。
  - **正则表达式**：任何不属于上述保留关键字的字符串都将被视为正则表达式，用于过滤动态文本内容。

**2. 提醒选项（控制群聊 @ 提醒）**

  - `at_all`：开启后，在群聊中检测到该 UP 主发布动态时，将尝试 `@全体成员`。
  - `at_sub`：设置后，会将当前发送指令的用户加入特定提醒列表。UP 主推送动态时会专门 `@该用户`。
  - `unat_sub`：取消当前用户对该 UP 主的特定 `@` 提醒。

> **⚠️ 注意**：
> 使用 `at_all` 需要发送指令的用户拥有群管理员及以上权限，且机器人自身也必须具备群管理员权限，否则将降级为普通推送。

**示例**：
`/订阅动态 123456 lottery 关注`
`/bili_sub 123456 lottery 关注`
这条指令会订阅 UID 为 `123456` 的 UP 主，但会过滤掉**抽奖动态**以及动态内容中包含“**关注**”二字的动态。

> **提示**：该指令也用于更新已订阅 UP 主的过滤条件。

## 适用平台/适配器

  - aiocqhttp
  - telegram
  - weixin_oc

## 常见问题

1. 渲染图片失败  
一般是公共接口不稳定性导致，详见[issue43](https://github.com/Soulter/astrbot_plugin_bilibili/issues/43)

2. 错误代码-352 / 412  
先查看以下issue中解决方案[issue34](https://github.com/Soulter/astrbot_plugin_bilibili/issues/34)、[issue58](https://github.com/Soulter/astrbot_plugin_bilibili/issues/58)、[issue72](https://github.com/Soulter/astrbot_plugin_bilibili/issues/72)

3. AstrBot更新到4.0版本后订阅失效  
UMO结构发生了变化，已为"全局列表"指令添加了具体订阅信息，使用该指令查看后重新订阅即可。  
简便的方法是进入 `data/plugin_data/astrbot_plugin_bilibili_aikaid` 文件夹修改 UMO 的第一部分（使用 `/sid` 指令了解区别）。

4. 使用新渲染模板发不出图片  
由于图文动态布局采用了纵向布局，如果图片过长，受限于qq本身机制，需以文件形式发送。  
你很可能需要在AstrBot"配置文件-系统配置"配置"对外可达的回调接口地址"

5. 生成的图片被错误裁剪或有多余区域  
始终推荐[自部署](https://docs.astrbot.app/others/self-host-t2i.html)，并且由于t2i服务更新，推荐及时更新到最新的镜像。  
本插件会始终在合适时支持更新的版本。

## 模板开发

详见[PR#53](https://github.com/Soulter/astrbot_plugin_bilibili/pull/53)
```bash
# 启动UI开发模式
cd astrbot_plugin_bilibili
python dev_ui.py
```

[astrbot-t2i-playground](https://github.com/AstrBotDevs/astrbot-t2i-playground) 也可以帮助开发和调试模板。

## Contributors

<a href="https://github.com/soulter/astrbot_plugin_bilibili/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=soulter/astrbot_plugin_bilibili" />
</a>

## 更新日志

见 [CHANGELOG.md](CHANGELOG.md)
