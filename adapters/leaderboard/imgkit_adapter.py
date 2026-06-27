"""
adapters/leaderboard/imgkit_adapter.py
排行榜图片生成适配器 - 实现 core/ports/leaderboard.py::LeaderboardGenerator

迁移自：libs/leaderboard_imge.py
"""
from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import imgkit

from core.domain.transfer import LeaderboardEntry
from libs.log import logger


MEDAL_EMOJIS = {
    1: "🥇",
    2: "🥈",
    3: "🥉"
}

MEDAL_EMOJI_OTHERS = "🪙"


class ImgkitLeaderboardGenerator:
    """
    使用 imgkit (wkhtmltoimage) 生成排行榜图片
    """

    def __init__(self) -> None:
        self._config = self._find_config()

    def _find_config(self) -> imgkit.config | None:
        """自动查找 wkhtmltoimage 可执行文件路径"""
        path = shutil.which("wkhtmltoimage")
        if path:
            return imgkit.config(wkhtmltoimage=path)

        common_paths = [
            r"C:\Program Files\wkhtmltopdf\bin\wkhtmltoimage.exe",
            r"C:\Program Files (x86)\wkhtmltopdf\bin\wkhtmltoimage.exe",
            "/usr/bin/wkhtmltoimage",
            "/usr/local/bin/wkhtmltoimage",
            "/opt/wkhtmltopdf/bin/wkhtmltoimage",
        ]
        for p in common_paths:
            if os.path.exists(p):
                return imgkit.config(wkhtmltoimage=p)
        return None

    async def generate(
        self,
        entries: list[LeaderboardEntry],
        direction: str,
        owner_name: str = "",
    ) -> str | None:
        if not self._config:
            logger.debug("未找到 wkhtmltoimage，无法生成排行榜图片")
            return None

        if not entries:
            return None

        website = entries[0].website
        bonus_name = entries[0].bonus_name or "点数"
        
        # 构造表格行
        rows = ""
        table_title = "打赏" if direction != "pay" else "赏赐"
        
        for e in entries:
            rank = e.rank
            # 内部类名，用于区分前三名
            rank_row_class = f"rank-{rank}" if rank <= 3 else "rank-normal"
            
            display_uid = self._mask_tgid(e.user_id)
            
            # 对过长的昵称进行截断处理
            display_name = e.user_name
            if not display_name or display_name.lower() in ["untitled", "none", "null"]:
                display_name = f"用户{display_uid}"
                
            if len(display_name) > 12:
                display_name = display_name[:12] + "..."
            
            rows += f"""
            <tr class="{rank_row_class}">
                <td class="rank-cell"><span class="rank-num">{rank}</span></td>
                <td class="username-cell">
                    <span class="username">{display_name}</span>
                    <small class="userid">{display_uid}</small>
                </td>
                <td class="count-cell"><span class="count">{e.count} 次</span></td>
                <td class="amount-cell"><span class="amount">{e.total_amount:,.1f}</span></td>
            </tr>
            """

        html_str = self._get_html_template(website.upper(), table_title, bonus_name, rows, owner_name)
        
        temp_dir = Path("temp_file/leaderboard")
        temp_dir.mkdir(parents=True, exist_ok=True)
        file_path = temp_dir / f"leaderboard_{uuid.uuid4().hex}.png"
        
        try:
            # 渲染图片
            imgkit.from_string(
                html_str, 
                str(file_path), 
                config=self._config,
                options={
                    "format": "png",
                    "encoding": "UTF-8",
                    "quiet": "",
                    "width": "500",      # 强制页面宽度为500px，和原项目保持一致
                    "enable-local-file-access": "",
                }
            )
            return str(file_path)
        except Exception as err:
            logger.error(f"生成排行榜图片失败: {err}")
            return None

    def _mask_tgid(self, tgid: int | str) -> str:
        s = str(tgid)
        if len(s) <= 4:
            return s
        return s[:2] + "****" + s[-2:]

    def _get_html_template(self, site: str, title: str, bonus_name: str, rows: str, owner_name: str = "") -> str:
        from config.config import MY_NAME
        display_name = owner_name or MY_NAME
        count = len(rows.split('<tr')) - 1
        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                body {{
                    background: #f0f2f5;
                    margin: 0;
                    padding: 10px;
                    font-family: "Helvetica Neue", Helvetica, Arial, "Microsoft YaHei", sans-serif;
                    width: 480px;
                }}
                .card {{
                    background: white;
                    border-radius: 12px;
                    border: 1px solid #e1e4e8;
                    overflow: hidden;
                    box-shadow: 0 4px 12px rgba(0,0,0,0.08);
                }}
                .header {{
                    background: #1a2a6c;
                    background: -webkit-linear-gradient(to right, #1a2a6c, #b21f1f, #fdbb2d);
                    background: linear-gradient(to right, #1a2a6c, #b21f1f, #fdbb2d);
                    padding: 18px 10px;
                    text-align: center;
                    color: white;
                }}
                .title {{ font-size: 20px; font-weight: bold; margin-bottom: 4px; text-shadow: 1px 1px 2px rgba(0,0,0,0.3); }}
                .subtitle {{ font-size: 13px; opacity: 0.9; letter-spacing: 1px; }}
                
                table {{
                    width: 100%;
                    border-collapse: collapse;
                    table-layout: fixed; /* 强制固定布局，防止长内容撑开 */
                }}
                th {{
                    background: #f8f9fa;
                    color: #586069;
                    font-size: 12px;
                    padding: 12px 10px;
                    text-align: left;
                    border-bottom: 2px solid #e1e4e8;
                }}
                /* 斑马纹：偶数行加背景色 */
                tbody tr:nth-child(even) {{
                    background-color: #f9fbfd;
                }}
                td {{
                    padding: 12px 10px;
                    border-bottom: 1px solid #f1f1f1;
                    color: #24292e;
                    vertical-align: middle;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                }}
                
                .rank-cell {{ width: 50px; text-align: center; }}
                .username-cell {{ width: 180px; }} /* 固定用户名宽度 */
                .count-cell {{ width: 80px; text-align: center; }}
                .amount-cell {{ width: 120px; text-align: right; }}

                .rank-num {{
                    display: inline-block;
                    width: 28px;
                    height: 28px;
                    line-height: 28px;
                    text-align: center;
                    background: #f1f3f5;
                    color: #495057;
                    border-radius: 50%;
                    font-weight: bold;
                    font-size: 14px;
                }}
                
                /* 前三名奖章配色 */
                .rank-1 .rank-num {{ background: #FFD700; color: #856404; box-shadow: 0 0 5px rgba(255,215,0,0.4); }}
                .rank-2 .rank-num {{ background: #C0C0C0; color: #383d41; }}
                .rank-3 .rank-num {{ background: #CD7F32; color: #ffffff; }}
                
                .username {{ 
                    font-weight: bold; 
                    font-size: 15px; 
                    color: #0366d6;
                    display: block;
                    width: 100%;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                }}
                .userid {{ font-weight: normal; color: #6a737d; font-size: 11px; }}
                .count {{ font-size: 13px; color: #28a745; font-weight: bold; }}
                .amount {{ 
                    color: #d73a49; 
                    font-weight: bold;
                    font-size: 16px;
                    font-family: "Courier New", Courier, monospace;
                }}
                
                .footer {{
                    padding: 12px;
                    background: #f8f9fa;
                    text-align: center;
                    font-size: 11px;
                    color: #6a737d;
                    border-top: 1px solid #e1e4e8;
                }}
            </style>
        </head>
        <body>
            <div class="card">
                <div class="header">
                    <div class="title">🌟 {display_name} 的数据终端</div>
                    <div class="subtitle">>>> {site} {title}排行榜 TOP {count} <<<</div>
                </div>
                <table>
                    <thead>
                        <tr>
                            <th class="rank-cell">排名</th>
                            <th>用户信息</th>
                            <th style="text-align:center;">次数</th>
                            <th style="text-align:right;">{bonus_name}累计</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows}
                    </tbody>
                </table>
                <div class="footer">
                    📊 数据实时同步更新 · 祝您好运连连 ☘️
                </div>
            </div>
        </body>
        </html>
        """
