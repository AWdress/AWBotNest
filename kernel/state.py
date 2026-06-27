"""
kernel/state.py
内核运行时单例持有处。

为什么需要它：
入口以 `python main.py` 运行时，入口模块名是 __main__；而 webui.api 里 `import main`
会再次加载一份 main.py（模块名 main），两份模块的全局变量互相独立。
若把 accounts/runtime 存在 main 的全局变量里，API 侧读到的永远是 None。

解决：所有内核单例存放在本模块。main 启动时 set_kernel(...)，API 侧 get_kernel() 读取。
本模块只会被加载一次（模块名固定为 kernel.state），不存在重复实例问题。
"""
from __future__ import annotations

from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from kernel.account_manager import AccountManager
    from kernel.plugin_runtime import PluginRuntime

accounts: Optional["AccountManager"] = None
runtime: Optional["PluginRuntime"] = None
started_at: Optional[float] = None  # 平台启动时间戳（用于运行时长）


def set_kernel(acc: "AccountManager", rt: "PluginRuntime") -> None:
    """启动时注入内核单例"""
    global accounts, runtime, started_at
    import time
    accounts = acc
    runtime = rt
    started_at = time.time()
