"""测试环境隔离：运行时状态文件一律落在临时目录。

CreditStateStore / AccountStateStore 的路径在模块导入时就固定了，而单测会
真的写"积分耗尽""账号受限"记录——不隔离的话，跑一次测试就等于给线上账号
凭空打了标记（表现为某账号无故被轮转跳过），仓库里的状态文件也跟着变脏。
必须在任何被测模块导入之前设置，故放在 conftest 里。
"""

import os
import tempfile

os.environ.setdefault(
    "WAPI_STATE_DIR",
    os.path.join(tempfile.gettempdir(), "web-api-tool-tests"))
os.makedirs(os.environ["WAPI_STATE_DIR"], exist_ok=True)
