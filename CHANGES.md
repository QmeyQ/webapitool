# 修改说明（Modification Notice）

本仓库是开源项目 **Universal Web-to-API** 的**修改版（derivative work）**。

依 GNU Affero General Public License v3.0 第 5 条 (a) 款要求，在此作出显著声明：

> **本程序已被修改。** 修改者：QmeyQ。最后修改日期：**2026-11-07**。
> 修改自 **2026-11-02** 起持续进行。逐文件修改清单见本文件下方。

---

## 1. 基线与日期

| 项目 | 值 |
| --- | --- |
| 上游项目 | Universal Web-to-API（将任意 AI 网站转换为 OpenAI 兼容 API） |
| 上游许可证 | AGPL-3.0-only（`LICENSE` 原文未改动） |
| 基线快照 | `universalweb-api-main/`（上游原始文件副本，文件时间戳 2025-12-18） |
| 修改开始 | 2026-11-02（最早运行痕迹 `uploads/prompt_1793609214` = 2026-11-02 16:46） |
| 最后修改 | 2026-11-07 |
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
| `browser_core.py` | +6287 / −741 | 浏览器自动化核心大幅扩展：IDE 工具调用兼容（`ToolSchemaConformer` 对齐 OpenAI / Anthropic `input_schema` / Gemini `parameters` 三种工具声明风格；`ToolCallStreamParser` 支持 5 种工具调用文本格式；续聊保留 `tool_calls` 配对）；深度思考走 `reasoning_content` 独立通道，不再混入正文；流式头部净化（角色回显、"已深度思考"等 UI 叙述行不再泄漏）；长提示词附件上传与分片路由（FILE / CHUNKED 策略、附件可见性复查防止"带附件分片"）；多 UI 变体选择器兼容；DeepSeek "继续"按钮点击等站点专用处理。<br>**2026-11-05 网页链路修复**：`_resolve_profile_dir()` 按项目根解析账号 profile，绝对路径失效时自动改道并告警；`probe_page_state()` + `PageUnavailableError` 页面可用性守卫（区分错误页 / 登录墙 / 无 composer）；`_wait_for_composer()` 等待 SPA 渲染出输入区（含登录墙水合稳定期），替代导航后固定 `sleep(4)`；`_pick_upload_input()` 按 `accept` 与"是否属于当前聚焦 composer"选择上传口，修掉向 `accept=image/*` 的口注入 `.txt` 的静默失败；`_EDITOR_RESOLVE_INSTALL_JS`/`__pickEditor` 统一写入与读回的元素解析（多实例 composer 下不再"写 A 验 B"）；`_collect_upload_rejection()` 采集站点自身的拒绝提示；**页面可用性判定上移到策略阶段**（`resolve_tab_for_model` 里判一次即抛 `PageUnavailableError`），发送循环内的 `_guard_page_usable` 改为带 TTL 的缓存探测（`_page_state`）——此前放在 `_execute_fill` 里，而分片发送逐片调用它，96 片就是 96 次全页按钮扫描，越到后面越贵；`_EDITOR_RESOLVE_INSTALL_JS` 因页面导航会清空 window 上的函数，仍保留在每次填充前（它是 `typeof` 判重的空操作，成本可忽略）；`_launch_instance_chrome()` 支持显式 `--proxy-server`；`switch_instance()` 切换前探测候选账号页面状态，登录墙/错误页一律跳过，并**不再 `quit()` 原实例窗口**（22:55 的整条链路挂死正是"一次瞬时零输出触发轮转 → 切到未登录账号 → 关掉唯一已登录窗口"造成的）；`_attachment_baseline()` / `_read_upload_files()` 让上一轮**以附件交付**的内容也进入压缩比对基准——附件轮次记录全文而非"请阅读文件 xxx"短模板，内存基准丢失时按登记的附件名或对话历史里的文件名从本地 `uploads/` 读回复原，读不到则安全退回全量发送；新增站点"上下文窗口已达上限"横幅检测（`probe_page_state` 返回 `context_full`/`limit_text`，判定要求"新聊天类按钮 + 其近邻容器内的上限文案"同时成立，避免把侧栏常驻的"新聊天"按钮或我们自己 prompt 里的"上下文"字样误判），命中后 `_handle_context_limit()` 作废压缩基准、清空会话附件计数、点击新聊天并把本轮当新会话全量重发；工作流点击新聊天与附件超限新起对话这两处也一并作废基准（否则占位符会指向新对话里并不存在的上文与附件，表现为"失忆"）；**长消息分片支持断点续传**——`_chunk_progress` 记录已送达片数与所属对话 URL，客户端超时重试时从下一片继续（不再重发已送达的片），存在有效断点时跳过"新聊天"步骤以留在同一会话，对话不一致或片数变化则安全地从头发送；新增单请求分片数上限 `MAX_CHUNKED_CHUNKS`（默认 30），超限的 prompt 在落盘/上传/分片之前就抛 `TooLongError` 并给出可执行提示（现场：1013810 字符需切 96 片，附件被站点拒收后回退分片，客户端每 30s 超时重试且每次都点新聊天，于是永远停在第 1、2 片）**2026-11-06 站点策略外置与账号受限处置**：判定与处置统一走 `site_strategy`（`probe_page_state` 只把状态翻译成人话），`_PAGE_STATE_JS` 新增 `banner`（alert/status/aria-live/composer 页脚）与 `limit_hits`（「重试」按钮近邻容器文案）两类语料——账号受限判定只吃站点提示区，绝不吃整页正文，因为正文里混着我们自己发出的 prompt（含 rate limit / usage limit / 上下文 之类字样），吃了就会把好账号误登记成受限；`resolve_tab_for_model` 在策略阶段按 `policy()` 处置：`usage_limited` 先登记当前账号冷却（ChatGPT 1 小时），再 `_rotate_to_usable_account()` 就地换到可用账号（此刻尚未发送任何内容，切号既不会丢上下文也不会重发，而 `main` 的重试循环在异常抛出后已没有机会重选账号），全部账号都在冷却时才带"哪些账号几点恢复"的原因失败；`switch_instance()` 的候选探测改为必须拿到结论性状态（`_probe_candidate` 会重探到 `ok` 才切，unknown/no_editor 一律跳过），并新增跳过冷却中的账号、顺手登记页面上正写着受限的候选账号；冷启动放宽登录墙判定（`_chrome_launched_ts`：Chrome 刚被自动拉起时水合更慢，稳定期 10s→45s、超时→90s），避免把登录着的账号误判成登录墙后切走——18:10 实测就是这个原因把唯一可用的账号放弃、切给刚启动的未登录实例，整条链路停在登录墙上。**同日回退**：工具表压缩 + `toollist` 索取补发方案撤销，工具表恢复逐字送达并固定豁免（`TOOL_TABLE_PROTECTED` 一并删除）——线上两次事故：AI 不回口令只回几个字（上游表现为"没有可用工具"），以及补发本身把请求打成零输出、触发刷新自恢复、会话被刷回新对话，客户端重试变成把十几万字重发一遍的死循环（11:43 实锤）。同日修复：`site_strategy` 文案表缺 `default` 条目导致陌生 domain 直接 `KeyError`，而 `_handle_context_limit` 的 `except` 把它吞成 `return False`，表现为"上下文已达上限"永不处理（单测抓到，判定函数现在对空/未知 domain 一律安全返回）；分片发送在最后一片之后重抓"发送前计数"，修掉 `[Fast AI Pre]` 拿上一片的短回执当本轮回复、导致只返回倒数第二条的问题。<br>**2026-11-06 同日第二批（真实链路跑出来的问题）**：账号 Chrome 一律经 `chrome_launcher` 起进**用户自己的登录会话**（`CreateProcessAsUser` + `winsta0\default` + 用户环境块），失败才退回本会话并明确警告"窗口不可见、无法交互登录"——服务以 LocalSystem 运行在 Session 0，那里 Popen 出来的 Chrome 端口通、进程在，但窗口画在无人看得见的服务桌面上，用户永远没法在这些 profile 里登录，于是所有账号看起来"莫名其妙没登录"（这台机器实测：5 个账号端口全在 Session 0，Session 0 里挂了 45 个 chrome 进程）；受限判定语料再加一道闸：ChatGPT 的**转录区自带 `aria-live`/`role=region`**，实测 banner 语料里混进了"你说：…／ChatGPT 说：…"即我们自己的 prompt 原文（中转把带 rate limit / usage limit / 请稍后再试 的报错日志原样贴进对话就会把好账号误登记成受限、打进一小时冷却），故 `inMsg()` 剔除 thread 转录容器与消息节点，`limit_hits` 也剔除对话回显；分片"被站点拒收"改为**归属判定**——只有拒收面板所在 turn 里含本片分片头才算我们这片被拒，旧面板一律按已送达继续（实测旧面板一直挂在最后一个 turn，导致 1970 字符那片原地降半重发十几次、346s 才失败、白烧十几条真实消息），并给降半重切加上次数上限与"已到下限仍被拒"即失败的出口；`_wait_for_composer` 在 Chrome 刚被自动拉起时放宽登录墙判定（稳定期 10s→45s、超时→90s）、`switch_instance` 候选必须探到结论性 `ok` 才切（`_probe_candidate` 重探到 12s，unknown/no_editor 一律跳过），因为 18:10 实测 10s 稳定期把登录着的账号判成登录墙并切给刚启动的未登录实例，整条链路停在登录墙上。<br>**2026-11-06 同日第三批（"消息打包"这条链抽出成 PromptPackager）**：`execute_workflow` 里原先散着四段互相牵制的逻辑——拼 prompt、压缩、体量拦截、路由与输入框文本，各记一半规则，所以"修好这里坏那里"。现在这四段的次序与规则由 `prompt_packager.py` 独占，browser_core 只调 `prepare()` / `record()` / `invalidate()`，并把基准查找、附件登记、分片断点三张表搬进这个类（`_fp_last_payload`/`_fp_attach`/`_chunk_progress` 留作只读别名，避免两处各存一份）。现场证据与修正：① 会话键位去掉 IDE 指纹成分，只用 `站点:对话id`——指纹随"最后一条消息"每轮都变，按指纹存基准就永远找不到，工作流于是每轮点「新聊天」把刚交付的几万字丢掉再整份重发（20:02 实测那份 **218,684 字符未压缩附件** 就是这么来的，模型读不动，上游就是"没有可用工具/权限不对"）；基准文本持久化到 `pack_state.json`，重启不再全量重发；② 先压缩、再按**压缩后**的大小决定 direct/file/chunked（原来按原文大小决定，压缩等于白做）；③ 键里没有真实对话 id 时（停在根地址、或历史遗留的裸指纹串）一律不存不取基准——两个都还没拿到 /c/id 的会话共用键会把别人的内容当成"本对话已交付"压掉，那是真丢内容；④ 删掉"拿 uploads 里最近的文件当基准"这一级：文件本身不带对话标识，OpenClaw 并发子代理时最近的文件很可能属于另一个对话；⑤ 附件轮次的输入框文本改为当场列出**工具名清单**并指明工具表在"文件开头"——旧措辞让模型去"对话开头/文件末尾"找，实测工具表在文件第 149 字符处，指错位置就让龙虾反复输出 `tool_search` 而不调用已声明的 12 个工具；⑥ `_cleanup_upload_dir()` 由"全删"改为保留最近 12 个（全删会断掉附件基准链，重启后同一对话每轮整份重发），start.bat 里的 `del uploads\prompt_*.txt` 一并撤掉，清理规则只留一处。真实两轮验证（同一网页对话、真实仓库源码文本）：轮 A 走 file 并由模型正确答出文件名，轮 B `基准来源=state` 且不再点新聊天，`策略=direct`。<br>**2026-11-06 同日第四批（"上下文里有没有这份附件"只能扫网页）**：新增 `attachment_names_in_page(tab)`（只认叶子节点里出现的 `prompt_*.txt`，含 composer 待发送的）与 `Package`/`PromptPackager` 的 `page_names` 入参；基准来源收窄为两种——① 本对话**已确认送达**的原文（以附件交付的，还要当场在页面上复核那个附件仍在，复核不到就基准作废、本轮整份发送），② 页面 DOM 里真实出现的附件名所对应的本地文件。新增 `mark_delivery(ckey, mode)` 记录这一轮实际是 text / chunked / attachment 交付的，改动之前写的无 mode 老记录一律按"待复核"处理。被删掉的两种错误依据：①"本地 uploads 里有同名文件"——现场实测该账号 4 个上传口全是禁用态，本地存着 8 个 prompt_*.txt 而网页对话里出现 **0 次**，那些都是上传失败回退留下的残档；②"IDE 重放的消息文本里提到了文件名"——那只证明客户端又把名字发回中转一次，与网页会话内容无关。两者都会让占位符指向 AI 根本看不到的附件，表现为"找不到附件、答非所问"。真实两轮复测：轮 A 扫出登记附件不在页面 → 日志"基准作废、本轮全量发送" → 走 file 且模型正确答出 `browser_core.py`；轮 B `基准来源=delivery`、未点新聊天、`策略=direct`。遗留的诚实边界：站点会不会重新读取**上一轮**那个仍在页面上的附件，本地无法证明，只能靠占位符里写明附件名让模型去找。顺带发现：该账号现在所有 `input[type=file]` 都是禁用态（"上传不可用…image/*(禁用)、空(禁用)"），附件一律落不上、只能回退分片直输——这是下一个要处理的问题。<br>**2026-11-06 同日第五批（待办 6：上传能力提前探测，不再每轮空烧）**：`_FILE_INPUT_FACTS_JS` 补上 `disabled` 字段（原先只看 `accept`，而站点把 4 个口全禁用时 accept 仍写着空/`image/*`，于是"注入成功"却永远没有附件——每轮白烧 12s 等待 + 一次页面刷新，刷新还会打断本轮发送）；accept/disabled/composer 归属的打分规则收成一处的 `upload_port_score()`，`_pick_upload_input()` 与新增的 `probe_upload_capability()` 共用（两处各写一份就会出现"探测说可用、真注入被静默丢弃"）；策略阶段 `_upload_capability(tab, domain)` 探测一次，判为缺失就调 `site_policy.mark_capability_missing(CAP_UPLOAD, …)` 登记 15 分钟，登记期内连探测都省掉；运行中真的上传失败一次也立刻登记。`PromptPackager.prepare(upload_available=False)` 从此不选 file/chunked-files，改走正文/分片直输，装不下才 `TooLongPlan` 并写明"请换有上传权限的账号"。同批修掉两处现场跑出来的错误判断：① `mark_delivery(mode!=attachment)` 时清掉之前失败尝试登记的附件名——留着它，下一轮的省略说明会写成"内容在附件 xxx 里"而那个附件从来没上过天（实测模型回"请发送完整消息。"）；② 压缩加下限——本来一条消息就装得下的内容（≤ `chunk_limit`）不压，4,051 字符压到 108 字符只会把任务本体从模型眼前拿走，压缩的价值在避免十几万字符切几十片。真实验证：4k 字符两轮均答对（38.0s / 33.8s，改动前 47.6s 且每轮一次无效上传尝试），30,096 字符无上传能力 → 分片直输 74.9s 返回本轮最终回复 `CHUNKFINAL-3172`。<br>**2026-11-06 同日第六批（站点级长内容设置进面板）**：取值优先级收成一处 `site_setting(站点键, 全局键, 默认)`——站点配置 → `browser_config.json`/`.env`（`BrowserConstants`）→ 代码默认，`''`/缺失都算"跟随上层"（面板留空就是这个意思），并把原先三处各写一遍的 `CHUNK_CHARS`/`DIRECT_CHUNK_CHARS`/`MAX_CHUNKED_CHUNKS` 兜底链统一（有一处漏读站点键，面板改了不生效）。新增可配项：`CONDENSE_MIN_CHARS`（压缩下限，默认 0=跟随分片大小）、`CONDENSE_ENABLED`（该站压缩总开关），`MAX_CHUNKED_CHUNKS` 支持站点覆盖并经 `context` 传给分片发送路径；`PromptPackager.prepare()` 相应新增 `condense_enabled` / `condense_min` 入参，日志把生效的 `压缩下限=` 一并打出来。真实验证：用面板同一套接口 `POST /api/config` 写 `CONDENSE_MIN_CHARS=2000` → 运行中的服务热重载后日志出现「压缩生效 4049 → 110 字符 … 压缩下限=2000」；改 `CONDENSE_ENABLED=false` → 「本轮不压缩：该站点已关闭压缩，4049 字符整份发送」且模型正常答对；随后 `git checkout sites.json` 还原，键无残留。另用真实 `normalizeConfig`（从 dashboard.js 抽出来在 node 里跑）做完整"面板保存"往返：保存前后逐键对比，丢失的键为无<br>**2026-11-07 第七批（分片投递「何时才允许等待」定成 spec）**：片间等待此前实现成"等上一条输出完全结束"，真实日志里每片要等 15~20s，6 片一条的消息光等待就 100s+，而且其中一次等满 120s 超时后那片仍判"发送未生效"，整条请求 181.16s 失败；反过来完全不等又会把片段丢在站点还在生成时（那时发送口根本不启用），模型只回一句"请继续发送剩余内容。"，几万字没进会话。现在按用户逐条给定的表实现，表本身写进代码注释当唯一规格，A/B 两套编号共用一个"上一条没回复 + 本条点不动"的**连续成立计数器**：A1 有回复+点得动→不等；A2 有回复+点不动→不等；A3 没回复+点得动→不等；A4 没回复+点不动→先照样点第二次，第二次判定时还符合 A4 才**等一次**，等完归零回到 A1 的节奏（因此绝不是每轮都等）；B1 没回复+点得动→回到 A；B2 没回复+点不动→再点一次；B3 第二次还符合 B2→等一次，等完回到 A。两个新探针：`_chunk_btn_visible()`（数一眼可见且未禁用的发送按钮，绝不为此而等）与 `_chunk_prev_replied()`（最后一条有没有正文字数，有一点就算"已回复"）。投递循环同时改成：不再向外 yield（站点提示先攒进 `pending_msgs`，整份送完再一次性交出去，否则客户端一断连生成器被 close 就掐断投递）、刻意不查客户端取消标志（"停止"的本意是别再等回答，不是别再送内容）、`_wait_reply_queue_drained` / `_send_chunk_closing_nudge` / `CHUNK_CLOSING_NUDGE` 三处只为"读回执"存在的 59 行删除；`CHUNK_SEND_GAP`（默认 0.8s，站点级可配）只留人间间隔，且修掉 `context.get(...) or 默认` 把显式 0 吞成默认值的写法。**分片头重写**：每一片都写明"共 n 片、前面已到几片、这 n 条合起来才是一条完整内容"，最后一片明确"已全部发送完毕，请把第 1 片到第 n 片当作一份内容统一处理"——不写清楚时模型只对眼前这一片回应，几万字在它眼里从来没有合成过一条消息（2026-11-07 用户实测原话："ai 不知道前文有分片，每次回复都是对上一片的，这不对"）。两处按真实数据纠偏：① 第 1 片没有"上一条"，不参与"连续两次"计数——把它算进去会让第 2 片凭空等一次，而那时上一片才发出去 1 秒；② "上一条没回复"取的是**下一次点击那一刻**的快照（站点还在生成本来就算还没回），按用户要求这不构成等待理由，所以只有真的连续两次"没回复 + 没按钮"才等那一次，等完直接点、不再回头等。真实主流程验证（`openclaw agent` 走 `chatgpt/chatgpt.com` 跑真实工具轮，临时关掉该站附件路由逼出分片直输，跑完已还原）：59,371 字符切 6 片，11:20:01→11:20:59 全部送达，片间 11~13s（只有填充与拒收复查，没有任何一次"等输出"），该轮 88.99s 完成；紧接的 29,997 字符切 3 片 63.48s 完成，模型按工具结果答出 `SITE-READ-OK 328`。单测 `tests/test_chunked_send.py` 把这张表逐行走了一遍（`test_blind_gate_waits_only_on_the_second_hit` 等），并把假网页改成按 turn 判拒收（原来的 fake 只看最后一次填充长度，等于绕过了真实判定条件）。 |
| `main.py` | +1140 / −64 | FastAPI 入口扩展：`faulthandler` 崩溃落盘、多账号实例管理接口（`/api/instances` 增删与启动）、长消息分片等运行期配置项、与 `RequestManager` 的并发控制集成调整。**2026-11-05**：新增 `_chrome_proxy_args()`，两处 Chrome 启动均带上显式代理（服务以 LocalSystem 运行时读不到用户 HKCU 的 `ProxyServer`）；`_recover_page()` 遇到错误页（机器级故障）直接短路，不再轮转账号并 kill/重启浏览器；`PageUnavailableError` 单独上报真实原因；取标签页改用 `BrowserCore.current_tab()`（此前 `browser.tab` 恒为 `None`，"零输出刷新自恢复"从未真正执行）；`_cleanup_upload_dir()` 在应用启动时清理 `uploads\prompt_*`（服务由 nssm 直接跑 `python main.py`，不经 start.bat，故清理必须落在 lifespan 里才生效）；日志按用途分档：新增 `LOG_ROOT` 与 `_log_dir(category)`，运行日志写 `log/app/server.log`、崩溃现场写 `log/error/faulthandler.log`（`log/` 整体不入库，目录运行时自建）。**2026-11-06**：`/api/instances` 每个账号新增 `limited_sites`（账号级用量受限的解除时间，独立于积分记录，否则账号"无故被跳过"看不出原因）；`DELETE /api/credit-state` 新增 `kind=limited` 手动解除受限冷却；`PageUnavailableError` 上报时附上正在冷却的账号与恢复时刻（账号级换号已在策略阶段尝试过，走到这里说明没有可用账号，报错要能回答"那我该等什么"）。**2026-11-06 同日第五批**：`/api/instances` 每账号新增 `caps_missing`（如"该账号无上传权限，几点恢复"，来自 `site_caps.json`）；`DELETE /api/credit-state` 新增 `kind=cap`（`capability` 指定哪种能力）手动解除缺失登记，让面板上能立刻重试附件；启动清理 uploads 保留最近 12 个（`_cleanup_upload_dir(keep_newest=12)`）；`WAPI_NO_FILE_LOG` 让测试进程不再挂文件日志 handler（否则测试日志混进 `log/app/server.log`，取证时把测试造的会话当成真实流量） |
| `config_engine.py` | +86 / −14 | 选择器校验支持多 UI 列表变体（逐条校验、只剔除无效条目）；新增 `normalize_selector_group()` 将选择器列表归一化为 CSS 逗号分组串；配置快照 `_normalized_snapshot()` 统一输出字符串形态，保证下游消费者假设不变 |
| `data_models.py` | +199 / −297 | 动作类型新增 `UPLOAD_FILE`；选择器字段新增 `upload_btn`、`file_input`（DrissionPage 经 CDP 注入 `<input type="file">`）；类型定义与注释精简重构 |
| `request_manager.py` | +79 / −30 | 请求队列 / 并发锁 / 取消信号 / 客户端断开检测逻辑扩展 |
| `sites.json` | +161 / −53 | 站点配置扩充：选择器修正与补全、ChatGPT 站点 `input_box` / `send_btn` 增加第二 UI 变体（列表形式）、工作流步骤调整 |
| `dashboard.html` | +267 / −1 | 控制台界面新增"长消息分片"设置区与多账号实例面板；**2026-11-06** 实例状态不再谎报"运行中"——区分 `● 运行中` / `● 运行中（窗口不可见）`（并显示窗口进程号与所在会话），新增"重开可见窗口"按钮与"账号受限 · 几点恢复"标签（可点 × 立即解除冷却）；**同日第六批**：原"✂️ 长消息分片"单行面板扩成 **📦 长内容：压缩 / 分片 / 附件**，每站点单独可配 6 项——允许压缩（`CONDENSE_ENABLED`）、压缩下限（`CONDENSE_MIN_CHARS`）、分片大小（`CHUNK_CHARS`）、单请求最多片数（`MAX_CHUNKED_CHUNKS`）、允许存附件（`upload_config.enabled`）、走附件阈值与附件体积上限（`upload_config.max_prompt_length` / `max_file_size`）；留空即跟随全局默认，并实时显示"当前生效值"；另有一键"全部恢复默认"清除本站的所有覆盖。**同日再改**：面板改名 **🧩 站点配置**（不叫"长内容"，它就是这一站的配置）并与选择器/工作流一致做成可展开收起的折叠块（chevron + 点击标题栏切换，标题后显示"几项已覆盖 / 全部跟随全局默认"——只统计与全局默认不同的值，`ensureUploadCfg` 补齐的默认键不算覆盖）|
| `dashboard.js` | +188 / −2 | 实例状态 15 秒轮询、`/api/instances` 调用、超长提示词（`toolong`）前端提示、按模型/账号清理记录；**2026-11-06** 清理记录支持 `kind=limited`，启动窗口支持 `take_over=true`，窗口不可见时按错误级提示而非成功；**同日第六批修掉一个静默毁配置的 bug**：载入时 `normalizeConfig()` 只保留 `selectors/workflow/stealth` 三个键，其余站点级配置（分片大小、压缩下限、`upload_config`、`entry_url`…）一载入就被丢掉，再点"保存"就把残缺对象整份写回服务器——表现就是"设置改了又自己变回去"。现在原样展开保留，并补齐复选框默认值（键不存在时若显示"未勾选"，用户只是打开面板再保存就会写入 `CONDENSE_ENABLED=false`，等于不知不觉关掉该站压缩）|
| `browser_config.json` | +32 / −3 | 新增 `DEFAULT_SITE` 与 `INSTANCES` 多账号定义（名称、CDP 端口、`user_data_dir`）。**2026-11-05**：`user_data_dir` 改为项目内相对路径 `chrome_profiles\accN`（旧绝对路径指向已不存在的目录，导致 Chrome 静默新建未登录空 profile）；新增 `PROXY_SERVER`，供服务以 SYSTEM 身份启动 Chrome 时显式走代理 |
| `start.bat` | +16 / −0 | 启动前创建 `uploads` 目录、输出 `UPLOAD_DIR` 提示、清理旧 `server.log`；**2026-11-05** 增加 `del /q /f "uploads\prompt_*.txt"` 清掉上一轮落盘提示词；创建 `log/{app,error,supervisor,archive}` 分类目录，清理目标改为 `log\app\server.log`（并顺手删除根目录遗留的旧 `server.log`） |

## 4. 本次修改新增的文件（原项目中不存在）

程序相关新增源码：

- `msg_condense.py` — 会话内容压缩引擎：全部内容拼接完成后对整段做一次从头到尾的
  线性扫描，与已交付内容逐字相同的连续段压成 `首两字..末两字`，不同的内容原样留在
  原位（相同段可以出现任意多段，中间被不同内容打断也照样压）；
  `protect_prefix` 豁免协议区（工具表逐字送达）、`ref_note` 支持"省略内容在上一轮
  附件里"的措辞，使附件轮次也能参与压缩比对
- `site_strategy.py` — 站点级应对策略集中管理：`classify()` 把页面事实归一成
  `ok / unreachable / need_login / no_editor / context_full / usage_limited`，
  `policy()` 给出该状态的处置（是否登记账号冷却、是否切号、是否重开对话、冷却时长），
  `AccountStateStore` 持久化受限冷却到 `site_state.json`（与积分耗尽记录分开：
  受限按小时恢复，积分按天重置）。此前这些判定散落在 browser_core 各处，
  改一处漏一处，新增站点还得再抄一遍。
  文件名不叫 `site.py`：标准库有 `site.py`，同名会遮蔽它，pip/venv 一导入就炸。
  另含**站点能力缺失登记**（`CAP_UPLOAD` + `capability_state`，独立文件
  `site_caps.json`，默认 15 分钟）：账号此刻没有上传权限与"账号受限"语义不同
  ——不一定要切号（别的账号可能同样没有），但必须提前知道，否则每个长
  prompt 都要先空烧一次上传尝试
- `prompt_packager.py` — 「消息文本 → 压缩 → 打包策略」这条链的唯一归属地：
  `PromptPackager.prepare()` 保证先压缩、再按压缩后的大小决定 direct/file/chunked；
  基准键位只用 `站点:对话id`（不含 IDE 会话指纹）并把交付原文持久化到
  `pack_state.json`；"上下文里有什么"只认**现场扫网页 DOM 得到的附件名**与
  本对话已确认送达的原文（附件交付的还要页面复核，`mark_delivery()` 记录
  实际交付方式是 text / chunked / attachment）；没有真实对话 id 的键一律
  不存不取基准；附件轮次的输入框文本当场列出工具名清单并指明工具表在
  文件开头；`record()/invalidate()/migrate()/progress()` 统管已交付知识
  与会话重开作废；`prepare()` 的 `condense_enabled` / `condense_min` /
  `upload_available` / `page_names` 四个入参对应面板上的站点级设置，
  次序（先压缩后定策略）与下限都在日志里打出实际生效值
- `send_verify.py` — 发送结果校验
- `page_probe.py`、`_page_check.py`、`_poe_probe.py` — 页面结构与站点探测辅助脚本
- `chrome_launcher.py` — 跨会话启动与 Windows 会话取证：`launch_in_user_session()`
  用 `WTSQueryUserToken` + `CreateProcessAsUser`（`winsta0\default` + 用户环境块）
  把 Chrome 起进当前登录用户的桌面（服务在 Session 0，普通 Popen 的窗口没人看得见），
  并返回"起在哪个会话、pid 多少"；`window_status()` / `port_owner_pid()` /
  `find_profile_chromes()` 用一次 PowerShell 调用批量查端口归属与会话，
  供仪表盘说实话；`kill_pid()` 用于收回不可见的旧窗口
- `_qoder_site_probe.py`、`_qoder_state_check.py`、`_qoder_banner_check.py`、
  `_qoder_banner_where.py`、`_qoder_win_probe.py` —
  现场取证：读取真实页面上的受限/上限提示原文及其 DOM 位置、定位哪类节点把
  对话原文漏进提示语料、把真实页面喂给 `probe_page_state` +
  `site_strategy.classify` 看判定结果、查各调试端口窗口的真实会话（只读，不改页面）
- `_qoder_live_check.py`、`_qoder_chunk_check.py`、`_qoder_toggle_longprompt.py` —
  真实数据链路验证：用仓库真实源码文本作 prompt 走完整 HTTP 链路
  （含"正文里有 rate limit 字样"的误判防护场景）、强制走分片直输验证
  "返回的是最后一条回复"、以及临时开关附件路由的还原式配置脚本
- `_qoder_visible_check.py` — 验证请求触发的自动启动是否起在可见会话
- `_qoder_perm_probe.py`、`_qoder_perm_hits.py` — 抓取窗口上真实对话里权限/工具类
  原文，定位"龙虾老是权限不对"到底卡在什么话术上（只读）
- `_qoder_packager_live.py` — 真实两轮回放：同一网页对话里先大 prompt 后重放历史，
  验证"先压缩、再按压缩后大小决定打包"以及续传轮不再点新聊天
- `_qoder_attaches_in_page.py` — 现场核对：网页对话 DOM 里**实际**出现哪些附件名，
  与本地 uploads 差多少（判"上下文里有没有附件"只能这样扫，不能凭文件名猜）
- `_qoder_uploadcap_probe.py` — 现场核对：把每个 `input[type=file]` 的
  accept/disabled/composer 归属原样打出来并给出上传能力结论
  （判"这个账号此刻能不能上传"的原始证据）
- `_qoder_nocap_timing.py` — 真实计时验证：没有上传能力时连发两轮长 prompt，
  确认不再出现无效上传尝试与页面刷新，并记录每轮耗时
- `_qoder_sitecfg_live.py` — 真实验证站点级"压缩下限/压缩开关"：用面板同一套
  `POST /api/config` 写值 → 发真实请求 → 看日志里实际生效值，跑完还原 sites.json
- `_qoder_save_roundtrip.py` — 端到端复现"面板保存"这条路（GET /api/config →
  真实的 `normalizeConfig` 在 node 里跑 → POST 回去 → 逐键对比），
  证明保存不再丢站点级配置；`_qoder_normcfg_check.py` 单测该函数保留所有键
- `_service_guard.ps1` — 服务守护脚本
- `_verify_upload_fill.py` — 真实登录 profile 上的上传/填充链路验证脚本（独立端口，不动生产实例）
- `tests/` — `test_api_ide.py`、`test_chunked_send.py`、`test_dead_recovery.py`、`test_fast_ai_pre.py`、`test_fill_submitted.py`、`test_msg_condense.py`、`test_site_strategy.py`、`test_chrome_launcher.py`、`test_prompt_packager.py`、`test_upload_capability.py`、`test_tool_relay.py` 等测试
- `session_bindings.json`、`credit_state.json` — 运行时状态文件；
  `site_state.json`（账号受限冷却登记）、`site_caps.json`（站点能力缺失登记，
  如"该账号无上传权限"到几点复检）与 `pack_state.json`（每对话的压缩基准，
  **内含 IDE 提示词原文**，属于用户内容）同为运行时文件，机器相关，均不入库
- `.gitignore` — 忽略规则：上游基线快照 `universalweb-api-main/`、Chrome profile
  （`chrome_profiles/`、`chrome_profile/`）、`uploads/`（落盘提示词含用户原文）、
  `log/`（分类日志目录）、`site_state.json` 以及根目录散落的 `*.log` / `*.log.corrupt`

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
