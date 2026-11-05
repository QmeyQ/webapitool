# 修改说明（Modification Notice）

本仓库是开源项目 **Universal Web-to-API** 的**修改版（derivative work）**。

依 GNU Affero General Public License v3.0 第 5 条 (a) 款要求，在此作出显著声明：

> **本程序已被修改。** 修改者：QmeyQ。最后修改日期：**2026-11-05**。
> 修改自 **2026-11-02** 起持续进行。逐文件修改清单见本文件下方。

---

## 1. 基线与日期

| 项目 | 值 |
| --- | --- |
| 上游项目 | Universal Web-to-API（将任意 AI 网站转换为 OpenAI 兼容 API） |
| 上游许可证 | AGPL-3.0-only（`LICENSE` 原文未改动） |
| 基线快照 | `universalweb-api-main/`（上游原始文件副本，文件时间戳 2025-12-18） |
| 修改开始 | 2026-11-02（最早运行痕迹 `uploads/prompt_1793609214` = 2026-11-02 16:46） |
| 最后修改 | 2026-11-05 |
| 本仓库 | https://github.com/QmeyQ/webapitool （分支 `main`） |

日期依据为文件时间戳与运行产物时间戳；Git 历史已被压缩为单个提交，无法逐条回溯，故以上时间为准。

## 2. 许可证状态（AGPL-3.0 §5(b)、§5(c)）

- 修改后的整体程序**仍以 AGPL-3.0 授权**发布，未改为其他许可证，未依 §7 增加任何附加条款。
- 上游 `LICENSE`（AGPL-3.0 全文 661 行）与 `README.md` 中的许可证、免责声明章节**逐字保留，未作任何修改**，上游作者与贡献者署名保持原状。
- 本次修改新增的所有文件（见第 4 节）同样以 AGPL-3.0 授权，作为本整体程序的一部分。
- 已被修改的每个源码文件头部均带有指向本文件的修改声明。`sites.json`、`browser_config.json` 因 JSON 语法不支持注释，无法内嵌声明，其修改记录完全由本文件承担。

**§13（网络交互）提醒**：若你以修改版对外提供网络服务（本项目即为 HTTP API 服务），必须向这些用户免费提供对应的 Corresponding Source，可直接指向本仓库 `main` 分支。

## 3. 被修改的上游文件

相对基线 `universalweb-api-main/` 的差异行数（`+新增 / -删除`）：

| 文件 | 差异 | 主要修改内容 |
| --- | --- | --- |
| `browser_core.py` | +5100 / −784 | 浏览器自动化核心大幅扩展：IDE 工具调用兼容（`ToolSchemaConformer` 对齐 OpenAI / Anthropic `input_schema` / Gemini `parameters` 三种工具声明风格；`ToolCallStreamParser` 支持 5 种工具调用文本格式；续聊保留 `tool_calls` 配对）；深度思考走 `reasoning_content` 独立通道，不再混入正文；流式头部净化（角色标记回显、"已深度思考"等 UI 叙述行不再泄漏）；长提示词附件上传与分片路由（FILE / CHUNKED 策略、附件可见性复查防止"带附件分片"）；多 UI 变体选择器兼容；DeepSeek "继续"按钮点击等站点专用处理 |
| `main.py` | +893 / −62 | FastAPI 入口扩展：`faulthandler` 崩溃落盘、多账号实例管理接口（`/api/instances` 增删与启动）、长消息分片等运行期配置项、与 `RequestManager` 的并发控制集成调整 |
| `config_engine.py` | +86 / −14 | 选择器校验支持多 UI 列表变体（逐条校验、只剔除无效条目）；新增 `normalize_selector_group()` 将选择器列表归一化为 CSS 逗号分组串；配置快照 `_normalized_snapshot()` 统一输出字符串形态，保证下游消费者假设不变 |
| `data_models.py` | +199 / −297 | 动作类型新增 `UPLOAD_FILE`；选择器字段新增 `upload_btn`、`file_input`（DrissionPage 经 CDP 注入 `<input type="file">`）；类型定义与注释精简重构 |
| `request_manager.py` | +79 / −30 | 请求队列 / 并发锁 / 取消信号 / 客户端断开检测逻辑扩展 |
| `sites.json` | +161 / −53 | 站点配置扩充：选择器修正与补全、ChatGPT 站点 `input_box` / `send_btn` 增加第二 UI 变体（列表形式）、工作流步骤调整 |
| `dashboard.html` | +105 / −1 | 控制台界面新增"长消息分片"设置区与多账号实例面板 |
| `dashboard.js` | +93 / −2 | 实例状态 15 秒轮询、`/api/instances` 调用、超长提示词（`toolong`）前端提示、按模型/账号清理记录 |
| `browser_config.json` | +31 / −3 | 新增 `DEFAULT_SITE` 与 `INSTANCES` 多账号定义（名称、CDP 端口、`user_data_dir`） |
| `start.bat` | +4 / −0 | 启动前创建 `uploads` 目录、输出 `UPLOAD_DIR` 提示、清理旧 `server.log` |

## 4. 本次修改新增的文件（原项目中不存在）

程序相关新增源码：

- `msg_condense.py` — 消息压缩/整理
- `send_verify.py` — 发送结果校验
- `page_probe.py`、`_page_check.py`、`_poe_probe.py` — 页面结构与站点探测辅助脚本
- `_service_guard.ps1` — 服务守护脚本
- `tests/` — `test_api_ide.py`、`test_chunked_send.py`、`test_dead_recovery.py`、`test_fast_ai_pre.py`、`test_fill_submitted.py`、`test_msg_condense.py`、`test_tool_relay.py` 等测试
- `session_bindings.json`、`credit_state.json` — 运行时状态文件
- `.gitignore` — 忽略规则

以上文件均为本次修改的成果，同样以 AGPL-3.0 授权。

## 5. 未改动的上游文件

`LICENSE`、`requirements.txt`、`配置参数解释.md` 与基线逐字节一致。

`README.md` 的上游原文（简介、使用说明、免责声明、开源协议章节）逐字保留，未删除任何作者或贡献者署名，仅在标题下方插入了本修改版的显著声明块。

## 6. 如何核对

在本地（`universalweb-api-main/` 存在时）逐文件比对：

```sh
diff -u universalweb-api-main/browser_core.py browser_core.py
git diff --no-index --stat universalweb-api-main/ browser_core.py
```

列出一个文件的差异行数：

```sh
git diff --no-index --numstat universalweb-api-main/main.py main.py
```
