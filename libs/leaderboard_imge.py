# 标准库
import os
import uuid
from pathlib import Path

# 第三方库
import imgkit

# 自定义模块
from config import config
from libs.log import logger


medal_emojis = {
    1: "",
    2: "",
    3: ""
}

medal_emoji_others = ""

async def get_leaderboard(data, direction, owner_name: str = ""):
    # 配置 wkhtmltoimage 路径
    if os.name == "nt":
        wkhtmltoimage_path = r"D:\Tool Software\wkhtmltopdf\bin\wkhtmltoimage.exe"
        wkhtml_config = imgkit.config(wkhtmltoimage=wkhtmltoimage_path)
    elif os.name == "posix":
        # Linux环境下自动检测wkhtmltoimage位置
        import shutil
        wkhtmltoimage_path = shutil.which('wkhtmltoimage')
        if wkhtmltoimage_path:
            wkhtml_config = imgkit.config(wkhtmltoimage=wkhtmltoimage_path)
        else:
            # 尝试常见位置
            common_paths = [
                '/usr/bin/wkhtmltoimage',
                '/usr/local/bin/wkhtmltoimage',
                '/opt/wkhtmltopdf/bin/wkhtmltoimage'
            ]
            for path in common_paths:
                if os.path.exists(path):
                    wkhtml_config = imgkit.config(wkhtmltoimage=path)
                    break
            else:
                logger.error("未找到wkhtmltoimage，图片生成功能不可用")
                return None

    rows = ""
    table_title = "打赏" if direction != "pay" else "孝敬"


    for rank, uid, username, count, amount in data:
        emoji = medal_emojis.get(rank, medal_emoji_others)
        medal_img = f'{emoji} TOP{rank}'
        rank_class = f"rank top{rank}" if rank <= 3 else "rank"

        # 处理用户ID为None的情况
        if uid is None or uid == "None":
            display_uid = "未知"
        else:
            display_uid = mask_tgid(uid)

        rows += f"""
        <tr>
            <td class="{rank_class}">{medal_img}</td>
            <td class="userid">{display_uid}</td>
            <td class="username">{username}</td>
            <td class="count">{count}</td>
            <td class="amount">{amount}</td>
        </tr>
        """

    html_str = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{
                background: #667eea;
                font-family: Arial, sans-serif;
                padding: 4px;
                margin: 0;
            }}
            .container {{
                background: white;
                padding: 8px;
                width: 500px;
                border-radius: 4px;
                box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            }}
            .title {{
                text-align: center;
                color: #333;
                font-size: 20px;
                font-weight: bold;
                margin-bottom: 10px;
            }}
            .subtitle {{
                text-align: center;
                color: #666;
                font-size: 15px;
                margin-bottom: 12px;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                background: white;
                font-size: 14px;
                border: 1px solid #ddd;
                border-radius: 3px;
                overflow: hidden;
            }}
            thead {{
                background: #667eea;
            }}
            th {{
                padding: 8px 5px;
                text-align: center;
                color: white;
                font-weight: bold;
                font-size: 13px;
                border: 1px solid #555;
            }}
            th:nth-child(1) {{ width: 15%; }}  /* 排名列 */
            th:nth-child(2) {{ width: 20%; }}  /* 用户ID列 */
            th:nth-child(3) {{ width: 25%; }}  /* 用户名列 */
            th:nth-child(4) {{ width: 15%; }}  /* 次数列 */
            th:nth-child(5) {{ width: 25%; }}  /* 金额列 */
            tbody tr:nth-child(even) {{
                background: #f8f9fa;
            }}
            td {{
                padding: 6px 4px;
                text-align: center;
                color: #333;
                border: 1px solid #ddd;
                font-size: 12px;
            }}
            td:nth-child(1) {{ width: 15%; }}  /* 排名列 */
            td:nth-child(2) {{ width: 20%; }}  /* 用户ID列 */
            td:nth-child(3) {{ width: 25%; }}  /* 用户名列 */
            td:nth-child(4) {{ width: 15%; }}  /* 次数列 */
            td:nth-child(5) {{ width: 25%; }}  /* 金额列 */
            .rank {{
                font-weight: bold;
                font-size: 13px;
            }}
            .rank.top1 {{ color: #ffd700; font-size: 14px; }}
            .rank.top2 {{ color: #c0c0c0; font-size: 14px; }}
            .rank.top3 {{ color: #cd7f32; font-size: 14px; }}
            .userid {{
                color: #667eea;
                font-weight: 500;
            }}
            .username {{
                color: #333;
                font-weight: 600;
            }}
            .count {{
                color: #4ecdc4;
                font-weight: bold;
            }}
            .amount {{
                color: #ff6b6b;
                font-weight: bold;
                font-size: 12px;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="title">{owner_name or config.MY_NAME} 的赏赐数据终端 </div>
            <div class="subtitle">>>> TOP5 排行榜 <<<</div>
            <table>
                <thead>
                    <tr>
                        <th>排名</th>
                        <th>用户ID</th>
                        <th>用户名</th>
                        <th>次数</th>
                        <th>金额</th>
                    </tr>
                </thead>
                <tbody>
                    {rows}
                </tbody>
            </table>
        </div>
    </body>
    </html>
    """
    unique_id = uuid.uuid4().hex
    html_file = Path(f"temp_file/temp_{unique_id}.html")
    img_file = Path(f"temp_file/leaderboard_{unique_id}.png")
    html_file.parent.mkdir(parents=True, exist_ok=True)

    with open(html_file, "w", encoding="utf-8") as f:
        f.write(html_str)

    options = {
        'encoding': "UTF-8",
        'format': 'png',
        'width': 500,
        'enable-local-file-access': '',
        'quiet': ''
    }

    try:
        imgkit.from_file(str(html_file), str(img_file), options=options, config=wkhtml_config)
    except Exception as e:
        logger.error(f"图片生成失败: {e}")
        # 如果图片生成失败，返回None而不是抛出异常
        if Path(html_file).exists():
            Path(html_file).unlink()
        return None

    Path(html_file).unlink()
    return img_file



def mask_tgid(tgid):
    """
    对Telegram用户ID进行掩码处理，保护隐私

    Args:
        tgid: Telegram用户ID

    Returns:
        str: 掩码后的用户ID
    """
    if tgid is None:
        return "未知"

    tgid_str = str(tgid)
    if len(tgid_str) <= 4:
        return tgid_str  # 长度不足 5，直接返回原样

    # 对于较长的ID，显示前3位和后2位，中间用*代替
    if len(tgid_str) > 6:
        return f"{tgid_str[:3]}***{tgid_str[-2:]}"
    else:
        # 对于中等长度的ID，显示前2位和后1位
        return f"{tgid_str[:2]}**{tgid_str[-1:]}"