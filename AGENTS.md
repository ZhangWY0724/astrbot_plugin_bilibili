# AGENTS.md

本文件适用于仓库根目录及其全部子目录，供参与本项目的 AI Agent 和开发者遵循。

## 1. 项目定位

本项目是 AstrBot 的 Bilibili 订阅提醒插件，插件标识为 `astrbot_plugin_bilibili`。主要能力包括：

- 管理按会话（UMO）隔离的 UP 主动态及直播订阅；
- 轮询、过滤、渲染并向多个 AstrBot 平台推送通知；
- 管理 Bilibili 扫码登录及凭据持久化；
- 提供动态卡片模板的独立本地预览环境。

插件当前声明兼容 AstrBot `>=4.5.2`。除非需求明确要求提高最低版本，否则新增实现必须保持该兼容范围，不得直接采用更高版本才提供的 API。

## 2. 架构与职责

- `main.py`：唯一插件入口；负责 AstrBot Handler 注册、依赖组装、命令参数适配、后台任务启动和 `terminate()` 清理。不要在这里继续堆叠可独立测试的业务逻辑。
- `bili_client.py`：Bilibili 服务访问边界；负责凭据、代理、订阅数据请求及响应的基础校验。
- `services/subscription_service.py`：订阅新增和更新用例。
- `services/listener.py`：按 UID 调度轮询、动态解析、过滤、推送和 AI 摘要流程。
- `services/dispatcher.py`：AstrBot 会话消息分发、静默策略与发送结果。
- `services/renderer.py`：动态数据到文本或图片卡片的转换。
- `core/models.py`：领域数据模型和序列化契约。
- `core/data_manager.py`：订阅、凭据及运行状态的持久化；标准数据目录由 `StarTools.get_data_dir()` 获取。
- `core/constant.py`：跨模块常量、资源路径和模板注册表。
- `core/utils.py`：无状态通用辅助函数。
- `_conf_schema.json`：AstrBot WebUI 配置契约；字段名、类型、默认值必须与代码读取逻辑一致。
- `metadata.yaml`：插件市场元数据及兼容性声明。
- `skills/`（当前未使用）：仅在需求明确要求插件随包提供 AstrBot Skills 时新增；每个 Skill 使用独立目录及 `SKILL.md`，不得与本仓库的 AI Agent 协作说明混用。
- `assets/`：生产卡片模板和静态资源。
- `dev/`、`dev_ui.py`：模板本地预览与模拟数据，不得成为生产运行依赖。

## 3. AstrBot 开发约束

1. 插件入口类必须位于 `main.py` 并继承 `Star`。Handler 必须定义在插件类中，前两个参数保持为 `self`、`event`；复杂逻辑下沉至 `services/`、`core/` 或客户端模块。
2. 沿用项目现有 AstrBot 注册和事件返回风格。不要仅为追逐新版示例而批量替换 `@register`、过滤器导入或 `MessageEventResult` 用法；若确需迁移，必须先验证最低兼容版本。
3. Handler 使用 `async def`。网络、等待和消息发送必须异步；禁止新增 `requests` 或在事件循环中执行阻塞 I/O。无法避免的文件或 CPU 阻塞操作应使用 `asyncio.to_thread()`，并控制调用频率。
4. 后台任务由插件实例持有。新增任务必须支持取消，并在 `terminate()` 中取消且等待结束；`asyncio.CancelledError` 必须继续传播或被明确用于正常退出。
5. 一条消息、一个 UP 主或一个第三方接口的失败不得终止轮询主循环。仅在能够恢复的边界捕获异常，使用 AstrBot `logger` 记录必要上下文，禁止空捕获。
6. 持久化数据只能写入 AstrBot 数据目录，禁止写入插件源码目录。修改存储结构时必须兼容已有 JSON，并为缺失、旧版和异常字段提供明确迁移或归一化逻辑。
7. UMO 是订阅隔离和消息投递的稳定标识。不得假设所有平台拥有相同的 UMO 分段；新增平台逻辑应集中在适配函数中，并对私聊、群聊及旧数据分别验证。
8. 配置项新增或调整时，必须同步修改 `_conf_schema.json`、代码默认值和 README。Schema 的 `type`、`default`、`options` 必须与运行时类型一致；敏感凭据不得写入日志。
   AstrBot 更新 Schema 时会为缺失项补默认值，并移除 Schema 中已删除的配置项；删除或重命名字段前必须先设计兼容迁移，不能把旧字段留存当作默认行为。
9. 新增第三方依赖必须写入 `requirements.txt`，给出必要且不过度宽泛的最低版本。优先复用现有依赖和标准库，避免只为少量辅助逻辑引入新包。
10. 新增命令应提供简短 docstring，明确参数、权限和别名；管理员能力使用 `PermissionType.ADMIN`，不要仅在函数体中做弱权限判断。
11. 消息组件和平台特性需要考虑降级路径。`AtAll`、转发节点、文件和图片等能力不能假定所有 `support_platforms` 均支持。
12. 插件范围限定为登录、凭据持久化、订阅管理、定时检测和订阅提醒。新增独立查询、消息自动解析、推荐或 LLM Function Tool 前必须先获得明确需求确认。
13. 官方文档中的 KV 存储、`self.name` 数据目录等能力要求 AstrBot `>=4.9.2`。本插件仍声明兼容 `>=4.5.2`，因此不得直接迁移到这些 API；若确有必要，先确认需求并同步提高 `astrbot_version`。

## 4. 代码质量要求

- 遵循 KISS、YAGNI、DRY、SOLID；优先完成当前需求，不预建无实际调用方的抽象层。
- 保持现有模块职责。仅当逻辑具有独立业务含义或明显可测试性时拆分，不做与需求无关的大规模重构。
- Python 新代码补充准确类型标注。领域结构优先使用现有 dataclass/模型，不在模块之间传递含义不明的松散字典。
- 资源路径使用 `pathlib.Path` 或 `os.path` 构造，不依赖进程当前工作目录。
- 注释和 docstring 使用中文，与当前代码库保持一致；注释说明原因、约束或兼容背景，不复述代码。
- 用户可见文本使用简体中文，并保持已有指令名称、别名和错误提示兼容。
- 不记录 SESSDATA、Access Token、Cookie、二维码登录凭据、完整私聊标识等敏感信息。
- 不直接修改生成数据、真实插件数据或用户配置来完成测试。

## 5. 修改流程

1. 先阅读相关入口、服务、模型、配置和调用方，确认数据流后再修改。
2. 将改动限制在需求涉及的模块；遇到用户已有未提交改动时保留并与其兼容。
3. 修复缺陷时优先补充能复现问题的测试或最小验证场景；新增功能至少覆盖正常路径、无数据路径和第三方异常路径。
4. 涉及订阅和轮询时，同时检查：UID 去重、时间间隔、缓存上限、过滤条件、持久化时机、重复推送和任务取消。
5. 涉及渲染模板时，同步更新 `dev/mock_data.py` 的代表性场景，并通过本地预览检查长文本、缺图、多图、转发和异常数据。
6. 涉及公开行为时同步更新 README；涉及版本发布时同步更新 `CHANGELOG.md` 与 `metadata.yaml`，版本号遵循语义化版本规范。
   `metadata.yaml` 是插件市场声明的唯一事实来源；展示名、短描述、支持平台、最低 AstrBot 版本等对外字段变化后，README 必须同步，不保留相互矛盾的平台列表或版本说明。
7. 未经用户明确要求，不创建分支、不提交、不推送，也不修改远程仓库或插件市场状态。

## 6. 验证要求

根据改动范围执行最小但充分的验证，并在交付时说明实际执行结果：

```powershell
# 基础语法检查
python -m compileall -q .

# 配置 Schema 语法检查
python -m json.tool "_conf_schema.json" > $null

# 格式与静态检查（环境已安装 ruff 时）
ruff format --check .
ruff check .

# 项目存在测试后执行
python -m pytest

# 卡片模板本地预览
python "dev_ui.py"
```

- 只改文档：检查内容准确性、链接和命令即可。
- 改 Python：至少执行 `compileall`，并运行相关测试；提交前按 AstrBot 官方要求使用 Ruff 格式化。
- 改配置：解析 `_conf_schema.json`，核对每个变更字段的运行时读取与默认值。
- 改模板：启动预览服务器并检查相关模拟场景；生产环境图片渲染仍应在 AstrBot 本体内复验。
- 改 Handler、推送或平台适配：在 AstrBot 本体的 `data/plugins/astrbot_plugin_bilibili` 环境中热重载，检查插件加载日志和实际消息链。
- 改后台任务：验证重载/停用后没有遗留任务、重复轮询或重复推送。

当前仓库未提供自动化测试目录。对于共享模型、过滤、调度、持久化或平台解析的非简单变更，应新增聚焦的 `tests/` 用例；不得以手工验证替代所有可自动化的纯逻辑测试。

## 7. 发布检查

- `metadata.yaml` 的 `name` 保持唯一且与数据目录、注册名一致；`version` 使用语义化版本，不添加 `v` 前缀。
- 当前 `metadata.yaml` 的 `version: v1.6.4` 是存量格式。本次仅制定规则时不顺带修改；下一次明确进行版本发布时，应将其规范化为无 `v` 前缀，并同步 `CHANGELOG.md`，避免制造无业务内容的版本变更。
- 可按实际需要补充 `display_name`、`short_desc`、`social_link`、`tags`；不得为了填满字段而编造信息。插件提供 Skills 时，使用官方约定的 `skills/<skill-name>/SKILL.md` 结构，并将其作为发布包内容一并验证。
- `astrbot_version` 使用 PEP 440 范围；新增 API 超出当前最低版本时必须同步提高约束并在变更日志说明。
- `support_platforms` 只能使用 AstrBot 官方适配器标识，并与实际降级能力一致。
- `logo.png` 保持 1:1；插件发布压缩包不得超过 16 MB。
- 发布包不得包含 `.git`、`__pycache__`、本地虚拟环境、真实配置、凭据、运行数据或无关开发产物。
- 发布前确认依赖完整、README 可操作、配置迁移兼容、后台任务可关闭，并在 AstrBot 中完成一次加载与热重载验证。

## 8. 参考资料

- AstrBot 插件开发指南：<https://docs.astrbot.app/dev/star/plugin-new.html>
- 最小插件实例：<https://docs.astrbot.app/dev/star/guides/simple.html>
- 插件配置：<https://docs.astrbot.app/dev/star/guides/plugin-config.html>
- 插件存储：<https://docs.astrbot.app/dev/star/guides/storage.html>
- 发布插件：<https://docs.astrbot.app/dev/star/plugin-publish.html>

若官方文档与本文件冲突，以项目声明的最低 AstrBot 版本可用 API 和官方当前规范为准，并在修改本文件时记录兼容性原因。
