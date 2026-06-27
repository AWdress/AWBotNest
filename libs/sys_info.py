# 标准库
import os
import sys
import platform
import tomllib
from pathlib import Path

# 第三方库
from core import PYROGRAM_VERSION


pyproject_path = Path(__file__).resolve().parent.parent / "pyproject.toml"

async def system_version_get():
    container_name = os.getenv("HOST_NAME", "")
    sys_info = platform.uname()
    hostname = container_name or sys_info.node
    kernel_version = platform.uname().release
    
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)
    project_name = data["project"]["name"] or "unkown"

    python_info = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    pyrogram_info = f"{PYROGRAM_VERSION}" 
    tgbot_sate = (
        f"🚀 **{project_name} 项目运行状态**\n\n"
        f"🖥️ **系统信息**\n"
        f"├ 主机名: `{hostname}`\n"
        f"├ 平台: `{sys_info.system}`\n"
        f"└ 内核: `{kernel_version}`\n\n"
        f"🐍 **运行环境**\n"
        f"├ Python: `{python_info}`\n"
        f"└ Pyrogram: `{pyrogram_info}`\n\n"
        f"✅ **状态**: 运行正常\n"
        f"⏰ **启动时间**: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    return project_name, tgbot_sate