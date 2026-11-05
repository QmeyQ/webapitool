# USER.md - User Model

Store stable user preferences and profile facts as directives that can guide future sessions.

Use one directive per entry:

```md
<!-- observed: YYYY-MM-DD | status: active -->

- Prefer concise progress updates during implementation work.
```

- Begin each directive with an imperative such as `Always`, `Never`, or `Prefer`.
- Record the observation date and either `active` or `superseded` on the metadata line.
- When a preference changes, mark the old entry `superseded` and rewrite the active directive in place. Never append a contradictory active directive.
- Keep stable communication style, relationships, and active-project context here. Put durable non-profile facts and decisions in `MEMORY.md`.
- Save this file at the workspace root as `USER.md`. It loads every session with a separate 4,000-character budget.

## Directives

<!-- observed: 2026-11-04 | status: active -->

- Always 端到端实测每个修改点后才算完成：浏览器自动化相关的修改必须在真实网页上验证最终效果（检查页面 DOM、附件 chip、截图），Never 只凭日志的成功标志就报告完成——日志"成功"可能是假阳性（例：input() 无异常但打在已脱离 DOM 的旧节点上，附件实际没挂上）。验证链路：修改 → 重启服务 → 发真实请求 → 检查网页实际状态 → 确认效果可见后才汇报。
- Always 先质疑既有"成功"信号的可信度再下结论：去页面上看真实状态，不要拿自动化流程自己的输出当证据。
- Always 判定"发送失败/无回复/疑似限流"前必须先读网页端 AI 的实际回复内容（最后一条消息的完整文本），Never 在没看页面 AI 回复的情况下猜测"软限流/页面异常/账号被封"——上游 AI 大概率已经回复了（提示原因、要求补充、或已开始执行），中转没看到是提取问题，不是 AI 没回。排查顺序固定：读页面 AI 回复 → 再分析中转为何没检测到 → 最后才考虑限流等异常。
- Never 在浏览器层堆叠多个"自愈兜底"（补点/补 Enter/激活窗口/刷新重写）：多层恢复会互相叠加，把"一次发送失败"放大成"重复发送、重复粘贴"。发送动作只保留一个出口，失败就报错交给上层统一处理。
- Never 用诊断脚本（DrissionPage）调 p.quit()、Never 手动 Start-Process 拉起 chrome 实例（会用错 profile 拉出未登录窗口）——实例一律由服务/dashboard 拉起。
- Always 在用户正在看的窗口上执行任何操作前先想清楚：这个动作用户会肉眼看到什么（大段粘贴、页面跳转、窗口被激活），有没有必要，会不会与服务的其他流程冲突。

<!-- observed: 2026-11-04 | status: active -->

- Prefer 简洁直接的进度汇报；对关键结论给出可复核的证据（日志行号 / 页面状态）。

## Directives（会话/tab 路由，用户明确要求长期遵守）

<!-- observed: 2026-11-05 | status: active -->

- Never 切 tab：IDE 没有新建会话（同一会话继续发请求）时，必须续聊在已绑定的原 tab 原会话里，绝不允许路由/切换到其他 tab，也绝不新开 tab。
- Never 切 tab（新会话场景）：IDE 真正新开会话时，在当前 tab 上点站点页面里的"新聊天"按钮开始新会话，不要切到别的 tab、也不要为每个会话开新 tab（多会话并行隔离是例外，见 fp 绑定机制，但常规同站会话一律原地点"新聊天"）。

## Related

- [Agent workspace](/concepts/agent-workspace)
