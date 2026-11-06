"""测试环境隔离：运行时状态文件一律落在临时目录。

CreditStateStore / AccountStateStore 的路径在模块导入时就固定了，而单测会
真的写"积分耗尽""账号受限"记录——不隔离的话，跑一次测试就等于给线上账号
凭空打了标记（表现为某账号无故被轮转跳过），仓库里的状态文件也跟着变脏。
必须在任何被测模块导入之前设置，故放在 conftest 里。

日志同理：导入 main.py 会挂上写 log/app/server.log 的 handler，之后每条
测试日志都落进生产日志——排查线上问题时那些行看起来就是"真实请求"，
现场取证会被带偏（2026-11-06 实测：把测试造的会话当成龙虾的流量读了一轮）。
WAPI_NO_FILE_LOG 让 main 在测试进程里不挂文件日志。
"""

import os
import tempfile

os.environ.setdefault(
    "WAPI_STATE_DIR",
    os.path.join(tempfile.gettempdir(), "web-api-tool-tests"))
os.makedirs(os.environ["WAPI_STATE_DIR"], exist_ok=True)
os.environ["WAPI_NO_FILE_LOG"] = "1"
