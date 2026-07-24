#!/usr/bin/env python3
"""
版本号统一管理工具

用法：
  python scripts/update_version.py 1.2.3.4

功能：
  1. 更新 VERSION 文件
  2. 更新 CHANGELOG.md（添加新版本占位符）
  3. 更新 docs/API.md（添加新版本占位符）
  4. webui/api.py 和 webui/frontend/package.json 会自动从 VERSION 文件读取
"""
import sys
from pathlib import Path
from datetime import datetime


def update_version(new_version: str):
    """更新版本号到所有需要的位置"""
    root = Path(__file__).parent.parent

    # 1. 更新 VERSION 文件
    version_file = root / "VERSION"
    version_file.write_text(new_version, encoding="utf-8")
    print(f"✓ 已更新 VERSION 文件为 {new_version}")

    # 2. 在 CHANGELOG.md 开头添加新版本占位符
    changelog_file = root / "CHANGELOG.md"
    if changelog_file.exists():
        content = changelog_file.read_text(encoding="utf-8")
        lines = content.split("\n")

        # 找到第一个 ## v 开头的行（跳过文件头注释）
        insert_index = None
        for i, line in enumerate(lines):
            if line.startswith("## v"):
                insert_index = i
                break

        if insert_index is not None:
            # 插入新版本块
            new_section = [
                f"## v{new_version}",
                "",
                "待补充更新说明。",
                "",
                "**新功能：**",
                "- ",
                "",
                "**改进：**",
                "- ",
                "",
                "**修复：**",
                "- ",
                "",
            ]
            lines = lines[:insert_index] + new_section + lines[insert_index:]
            changelog_file.write_text("\n".join(lines), encoding="utf-8")
            print(f"✓ 已在 CHANGELOG.md 中添加 v{new_version} 占位符")

    # 3. 在 docs/API.md 的更新日志中添加新版本
    api_doc_file = root / "docs" / "API.md"
    if api_doc_file.exists():
        content = api_doc_file.read_text(encoding="utf-8")

        # 查找 ## 更新日志
        if "## 更新日志" in content:
            parts = content.split("### v", 1)
            if len(parts) == 2:
                today = datetime.now().strftime("%Y-%m-%d")
                new_entry = f"### v{new_version} ({today})\n\n- 待补充更新说明\n\n### v"
                content = parts[0] + new_entry + parts[1]
                api_doc_file.write_text(content, encoding="utf-8")
                print(f"✓ 已在 docs/API.md 中添加 v{new_version} 占位符")

    # 4. 同步 package.json（运行前端同步脚本）
    frontend_dir = root / "webui" / "frontend"
    sync_script = frontend_dir / "sync-version.js"
    if sync_script.exists():
        import subprocess
        try:
            subprocess.run(["node", str(sync_script)], cwd=str(frontend_dir), check=True)
            print(f"✓ 已同步 package.json 版本号")
        except Exception as e:
            print(f"⚠ package.json 同步失败（请手动运行 npm install）: {e}")

    print(f"\n✅ 版本号已统一更新为 {new_version}")
    print("\n下一步：")
    print("  1. 编辑 CHANGELOG.md 和 docs/API.md，补充本版本的更新说明")
    print("  2. git add -A && git commit -m '发布 vX.X.X.X'")
    print(f"  3. git push && git tag v{new_version} && git push origin v{new_version} --force")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("用法: python scripts/update_version.py 1.2.3.4")
        sys.exit(1)

    new_version = sys.argv[1]

    # 简单验证版本号格式
    parts = new_version.split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        print(f"错误: 版本号格式不正确，应为 X.X.X.X 格式（如 1.1.2.1）")
        sys.exit(1)

    update_version(new_version)
