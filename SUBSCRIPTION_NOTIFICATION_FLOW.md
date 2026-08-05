# Bilibili 动态订阅定时获取与提醒发送逻辑

本文档描述当前代码中的真实行为，作为后续调整视频、图文、文字、专栏和转发动态提醒内容与发送方式的基线。

## 1. 功能边界

当前插件只保留：

- Bilibili 扫码登录与凭据持久化。
- 按 UMO 隔离的 UP 主动态订阅。
- 按 UID 定时获取动态。
- 动态去重、过滤、渲染与发送。
- 普通动态的 `@全体成员` 和指定订阅者提醒。

当前不再包含直播状态检测、开播/下播提醒或动态 AI 总结。

代码支持的动态类型如下：

| 产品分类 | Bilibili 动态类型 | 当前处理方式 |
| --- | --- | --- |
| 视频 | `DYNAMIC_TYPE_AV` | 按视频动态处理 |
| 图文 | `DYNAMIC_TYPE_DRAW` | 按图文动态处理 |
| 文字 | `DYNAMIC_TYPE_WORD` | 与图文动态共用处理逻辑 |
| 专栏 | `DYNAMIC_TYPE_ARTICLE` | 使用专栏处理逻辑 |
| 转发 | `DYNAMIC_TYPE_FORWARD` | 组装当前转发内容和原动态 |

这些类型都来自同一次动态列表请求，然后在本地按类型分类。每个 UID 每个调度周期只需要一次动态接口请求。

## 2. 核心模块

| 模块 | 职责 |
| --- | --- |
| `main.py` | 插件初始化、登录、订阅命令、后台任务启动和终止 |
| `bili_client.py` | Bilibili 凭据构造、动态列表和 UP 主资料请求 |
| `services/subscription_service.py` | 新增或更新订阅，首次订阅时初始化动态游标 |
| `services/listener.py` | UID 调度、动态分类、过滤、提醒组装和发送协调 |
| `services/renderer.py` | 将动态数据转换为 `RenderPayload` 并渲染 HTML 卡片 |
| `services/native_opus_renderer.py` | 复用 Bilibili 凭据和代理，使用 Playwright 截取原生动态页面 |
| `services/dispatcher.py` | 重连静默判断、UMO 消息发送和发送结果封装 |
| `core/models.py` | 订阅记录、动态解析结果及渲染数据结构 |
| `core/data_manager.py` | 凭据、订阅游标和订阅配置持久化 |

## 3. 登录与任务启动

插件初始化时按以下优先级建立 Bilibili 客户端：

1. 优先读取插件数据文件中已持久化的完整凭据。
2. 没有持久化凭据时，使用配置中的 `sessdata`。
3. 两者都没有时，监听循环仍会启动，但每轮只记录缺少凭据的警告并等待。

`/bili_login` 扫码成功后会：

1. 把新凭据设置到当前 `BiliClient`。
2. 将凭据字典写入插件标准数据目录。
3. 取消旧监听任务并启动新任务。

`/bili_logout` 会：

1. 清除内存和数据文件中的扫码凭据。
2. 用配置中的 `sessdata` 重新创建 `BiliClient`。
3. 替换监听器持有的客户端并重启监听任务。

插件停用或重载时，`terminate()` 会取消并等待监听任务结束。监听循环收到 `asyncio.CancelledError` 后继续向外传播，从而正常完成清理。

## 4. 订阅数据结构

订阅数据按 UMO 隔离。当前结构可概括为：

```json
{
  "bili_sub_list": {
    "平台:消息类型:会话ID": [
      {
        "uid": 123456,
        "last": "最近处理的动态ID",
        "filter_types": [],
        "filter_regex": [],
        "recent_ids": [],
        "at_all": false,
        "at_sub_users": []
      }
    ]
  }
}
```

字段语义：

| 字段 | 含义 |
| --- | --- |
| `uid` | 被订阅 UP 主的 Bilibili UID |
| `last` | 最近处理过的动态 ID |
| `recent_ids` | 最近动态 ID 缓存，用于补充去重 |
| `filter_types` | 类型过滤项，命中的内容不提醒 |
| `filter_regex` | 正文正则过滤项，命中的内容不提醒 |
| `at_all` | 动态提醒尝试 `@全体成员` |
| `at_sub_users` | 提醒时需要单独 @ 的用户 ID |

旧数据中的 `is_live`、`live_atall` 和 `last_live_start_ts` 会在读取时被忽略，`filter_types` 中遗留的 `live` 也会被清除；这些变化会在该数据下一次保存时自然写回，不需要批量迁移。旧命令中的 `live` 和 `live_atall` 参数会被兼容性忽略，不会误存为正文正则。

所有记录通过 `StarTools.get_data_dir(plugin_name="astrbot_plugin_bilibili_aikaid")` 下的 JSON 文件保存。首次启动会从旧标识 `astrbot_plugin_bilibili` 的标准数据目录或历史相对路径复制数据，保留登录凭据、订阅和动态游标。保存操作使用 `asyncio.to_thread()`，避免同步文件写入直接阻塞事件循环。

## 5. 新增和更新订阅

### 5.1 参数解析

`/bili_sub <UID> [参数...]` 将参数分成三组：

- 类型过滤：`forward`、`lottery`、`video`、`article`、`draw`、`forward_lottery`。
- 提醒选项：`at_all`、`at_sub`、`unat_sub`。
- 其余字符串：作为正文过滤正则表达式保存。

过滤项表达的是“不发送”。例如 `video` 表示过滤视频，而不是只订阅视频。

### 5.2 首次订阅

首次订阅的处理顺序是：

1. 创建 `SubscriptionRecord` 并立即保存。
2. 拉取一次当前动态列表。
3. 使用与正式轮询相同的解析和过滤逻辑遍历当前动态。
4. 把得到的动态 ID 写入 `last` 和 `recent_ids`。
5. 不发送这些已有动态。

这一步会把订阅建立时已经存在的动态初始化为已处理，避免刚订阅就补发一批历史内容。

如果初始化请求失败，订阅本身仍然创建成功。下一次轮询可能将接口返回范围内的动态视为新内容。

### 5.3 更新订阅

同一 UMO 对同一 UID 再次执行订阅命令时，更新已有记录，不创建重复项。

只使用 `at_all`、`at_sub` 或 `unat_sub` 且未提供过滤项时，代码会继承原过滤规则。普通的重新订阅则会用本次提供的过滤规则替换原规则。

## 6. 定时调度

### 6.1 UID 级合并

每轮调度先把所有 UMO 下的订阅转换成：

```text
UID -> [(UMO, SubscriptionRecord), ...]
```

多个群或私聊订阅同一个 UID 时，动态列表对该 UID 只请求一次，随后针对各订阅记录分别过滤和发送。

### 6.2 调度周期

| 配置 | 默认值 | 语义 |
| --- | ---: | --- |
| `interval_secs` | 300 | 同一个 UID 完成一次检查后，到下一次可检查的间隔 |
| `task_gap_secs` | 20 | 两个不同 UID 任务之间的最小间隔 |
| `dynamic_limit` | 5 | 单个订阅在一次检查中最多发送的动态数量 |
| `recent_dynamic_cache` | 4 | 每个订阅保存的最近动态 ID 数量 |

监听任务启动时，当前所有 UID 都立即进入到期状态。每个 UID 执行完成后，以完成时间加 `interval_secs` 计算下一次运行时间。

没有订阅时每 2 秒重新检查一次。没有凭据时等待一个完整 `interval_secs` 后重试。调度器异常会记录堆栈并在 1 秒后继续，不会终止整个监听任务。

### 6.3 单个 UID 的请求

每次 UID 任务调用一次 `get_latest_dynamics(uid)`，获取视频、图文、文字、专栏和转发动态。

请求失败时记录日志，并继续调度其他 UID。一个 UMO 的处理失败也不会阻止同一 UID 的其他 UMO。

## 7. 新动态识别与通用过滤

### 7.1 新动态识别

接口返回动态列表后，代码执行以下判断：

1. 跳过缺少 `modules` 的异常条目。
2. 跳过带“置顶”标记的动态。
3. 从最新向旧遍历。
4. 遇到 `last` 或 `recent_ids` 中任一已知 ID 时停止继续向旧扫描。
5. 此前收集的条目视为本轮新动态。

解析结果会反转成从旧到新的顺序发送，尽量保持发布时间顺序。

### 7.2 通用处理规则

- 支持的动态生成 `DynamicParseResult.deliver(payload, dyn_id)`。
- 被过滤的动态生成带 `dyn_id` 的 `skip` 结果。
- 未支持的未知动态类型使用空 `dyn_id` 跳过，避免未知类型挤占正常动态缓存。
- 被过滤的已知动态仍会记录为已处理，后续不会反复检查。
- 超过 `dynamic_limit` 的动态不会发送，但仍会记录为已处理。

## 8. 视频动态逻辑

视频对应 `DYNAMIC_TYPE_AV`。

### 8.1 过滤

当 `filter_types` 包含 `video` 时，直接跳过并记录动态 ID。视频动态当前不应用用户自定义正文正则过滤。

### 8.2 内容组装

从动态数据中提取：

- UP 主名称、头像和头像挂件。
- 视频标题和 BV 号。
- 视频封面和动态附言。
- 视频链接及二维码。

纯文本模式根据 `DYNAMIC_TYPE_AV` 把动作显示为“投稿了新视频”。

## 9. 图文、文字和专栏逻辑

### 9.1 图文和文字动态

`DYNAMIC_TYPE_DRAW` 和 `DYNAMIC_TYPE_WORD` 共用处理函数：

1. 包含 `draw` 过滤时跳过。
2. 充电专属内容跳过。
3. 互动抽奖且包含 `lottery` 过滤时跳过。
4. 摘要正文命中 `filter_regex` 时跳过。
5. 其余内容进入提醒组装。

组装字段包括作者信息、标题、摘要正文、最多 9 张图片、动态链接和二维码。没有图片且启用图片卡片时，使用插件 Logo 作为占位图。

### 9.2 专栏动态

`DYNAMIC_TYPE_ARTICLE` 使用独立入口：

1. 包含 `article` 过滤时跳过。
2. 充电专属内容跳过。
3. 其余内容复用图文 `RenderPayload` 构造。

当前专栏分支不应用用户自定义正文正则过滤。

### 9.3 转发动态

`DYNAMIC_TYPE_FORWARD` 同时组装当前转发者内容和原动态内容。

过滤顺序：

1. 原动态是互动抽奖且配置 `forward_lottery` 时跳过。
2. 配置 `forward` 时跳过全部转发动态。
3. 当前转发正文符合开奖文案且配置 `lottery` 时跳过。
4. 当前转发正文命中 `filter_regex` 时跳过。

通过过滤后，外层保存当前转发者和转发正文，内层 `forward` 保存原作者、标题、正文和第一张原图。

## 10. 消息组装方式

### 10.1 渲染模式选择

`render_mode` 支持 `auto`、`plain`、`card` 和 `native`：

- `auto` 根据旧版 `rai` 布尔配置兼容为 `plain` 或 `card`。
- `plain` 直接组装文本消息。
- `card` 使用 AstrBot `html_render()` 生成插件自定义图片卡片。
- `native` 使用独立的 Playwright Chromium 打开 `https://www.bilibili.com/opus/{dyn_id}`，等待 `.bili-opus-view` 可见并对该元素截图。

原生模式失败降级为卡片，卡片失败再降级为纯文本。原生渲染器懒启动并复用 Chromium；每条动态使用独立页面，插件终止时关闭浏览器。

### 10.2 图片卡片模式

`render_mode=card` 时：

1. `Renderer` 将 `RenderPayload` 送入当前 HTML 模板。
2. 最多尝试渲染 3 次，每次失败后等待 2 秒。
3. 输出文件必须存在、大于 4096 字节且能被 Pillow 验证。
4. 图片高度符合平台限制时发送图片。
5. 图片过高时改为文件发送。
6. 在图片或文件之后追加动态链接。
7. 渲染完全失败时降级为纯文本消息。

### 10.3 原生动态截图模式

原生模式不改变动态 API 的新旧判断和过滤逻辑，只把已通过过滤的动态 ID 转换为固定的 `opus/{dyn_id}` 地址。浏览器上下文使用现有扫码登录或 `sessdata` 凭据对应的 Cookie，并复用 `proxy`。

首次启动时若缺少 Chromium，或启动错误表明 Linux 共享库缺失，`native_browser_install=auto` 会自动修复运行环境。Linux 执行 `python -m playwright install --with-deps chromium`，其他系统执行 `python -m playwright install chromium`；`manual` 只记录对应的手动安装提示；`disable` 不启动浏览器。安装或启动失败不会终止订阅轮询。

页面等待顺序为：DOM 加载、`.bili-opus-view` 可见、字体就绪、容器内图片加载、布局稳定，随后执行元素级 JPEG 截图。

浏览器上下文默认使用 `1920×1080` 视口和 `1` 倍设备像素比。视口用于触发标准桌面响应式布局，最终图片仍按 `.bili-opus-view` 的实际边界裁剪。

当前联调阶段会在截图前删除 `.v-popover-content` 并注入同选择器的隐藏样式，避免登录浮层再次遮挡动态主体；原生截图地址临时固定为 `opus/1232710769951375376`，不改变实际动态的轮询、去重、缓存键和消息投递目标。

### 10.4 纯文本模式

`render_mode=plain` 时默认格式为：

```text
📣 UP 主「作者」动作:
标题: 标题（有值时）
正文或摘要（有值时）
[远程图片组件...]
链接
```

若配置 `plain_push_template`，可使用 `{name}`、`{uid}`、`{action}`、`{title}`、`{text}` 和 `{url}`。

模板格式化异常时回退到默认格式。空行会被移除，图片仍作为独立消息组件追加。转发原文可以使用 `plain_push_forward_template` 单独格式化。

### 10.3 转发节点模式

`node=true` 时，最终消息链被包装为名为 `AstrBot` 的 `Node`，再交给 AstrBot 发送。该能力依赖具体平台支持。

## 11. @ 提醒逻辑

消息内容组装完成后，再在消息链前添加 @ 组件：

1. 若 `at_all` 生效，优先添加 `AtAll`。
2. 若没有成功使用 `AtAll` 且存在 `at_sub_users`，逐个添加指定用户 @。

`@全体成员` 仅支持群聊，并通过平台 `call_action` 检查机器人角色和群剩余次数。检查失败或权限不足时不发送 `AtAll`；如果配置了指定订阅者，则降级为 @指定用户。

## 12. 统一发送与重连静默

所有动态提醒最终转换为 `SubscriptionNotification`，包含目标 UMO、消息组件、是否使用转发节点和可选动态 ID。

`SubscriptionNotificationDispatcher` 使用 `context.send_message(UMO, result)` 发送。单个会话发送异常会记录日志并返回失败，不抛出到 UID 主循环。

启用 `reconnect_silent` 后，如果插件发现距离上次成功提醒超过阈值，会进入一段计算出的静默期。静默期内动态提醒会被直接丢弃，不调用平台发送。

当前调用链在静默丢弃、超过 `dynamic_limit` 或发送失败后，仍可能把对应动态 ID 更新为已处理，因此这些内容不会在下一轮自动重试。

## 13. 渲染缓存

动态基础消息链按 `dyn_id` 缓存在内存中。多个 UMO 订阅同一个 UID 时，后续会话复用已经生成的卡片或纯文本链，再按各自订阅配置添加 @ 组件，避免重复渲染。

缓存只存在于插件进程内，重载后清空。

## 14. 状态写入时机

- 可发送动态：调用提醒处理后更新动态 ID。
- 被规则过滤的动态：直接更新动态 ID。
- 超出本轮发送上限的动态：不发送但更新动态 ID。
- 未支持的未知类型：不更新动态 ID。

`update_last_dynamic_id()` 同时更新 `last` 和 `recent_ids`，然后保存整个数据文件。

## 15. 失败隔离

- 一个 UID 请求失败，不终止其他 UID 的后续调度。
- 一个 UMO 处理失败，不阻止同 UID 的其他 UMO。
- 一条动态渲染失败，降级为纯文本。
- 一次平台发送失败，记录日志并继续监听。
- 模板格式化失败，回退到默认纯文本格式。

## 16. 当前流程图

```mermaid
flowchart TD
  A["插件启动或登录成功"] --> B["启动 DynamicListener"]
  B --> C{"存在有效凭据？"}
  C -->|否| D["等待 interval_secs"]
  D --> C
  C -->|是| E["按 UID 合并所有 UMO 订阅"]
  E --> F["选择到期 UID"]
  F --> G["获取一次动态列表"]
  G --> H["识别新动态并过滤置顶/已处理项"]
  H --> I{"动态类型"}
  I -->|视频| J["组装视频 RenderPayload"]
  I -->|图文/文字/专栏| K["组装内容 RenderPayload"]
  I -->|转发| L["组装外层与原动态"]
  J --> M["图片卡片或纯文本"]
  K --> M
  L --> M
  M --> N["添加 @ 组件"]
  N --> O["Dispatcher 按 UMO 发送"]
  O --> P["记录动态 ID"]
  P --> Q["等待该 UID 的下个周期"]
```

## 17. 后续修改提醒时的稳定边界

后续如果只调整提醒内容和发送方式，建议保持以下行为不变：

- 登录凭据及订阅 JSON 结构。
- UID 级请求合并和调度周期。
- `last` 与 `recent_ids` 去重规则。
- 现有过滤关键词及其“不发送”语义。
- UMO 隔离和统一 Dispatcher。
- 单个 UID、单个会话和单次渲染失败的隔离。

优先改造范围应限定在：

- 各类型如何生成统一的提醒内容模型。
- 视频、图文、文字、专栏和转发分别展示哪些字段。
- 图片、原图、文本、链接和二维码的排列方式。
- 图片卡片、纯文本及平台降级的目标格式。
