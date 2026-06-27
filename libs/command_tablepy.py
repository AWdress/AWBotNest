# 标准库
import os
import shutil
import uuid
from pathlib import Path

# 第三方库
import imgkit

# 自定义模块
from libs.log import logger


def _find_wkhtmltoimage() -> str | None:
    """自动查找 wkhtmltoimage 可执行文件路径"""
    # 优先用 shutil.which 搜索 PATH
    path = shutil.which("wkhtmltoimage")
    if path:
        return path

    # 常见安装路径
    common_paths = [
        # Windows
        r"C:\Program Files\wkhtmltopdf\bin\wkhtmltoimage.exe",
        r"C:\Program Files (x86)\wkhtmltopdf\bin\wkhtmltoimage.exe",
        # Linux / macOS
        "/usr/bin/wkhtmltoimage",
        "/usr/local/bin/wkhtmltoimage",
        "/opt/wkhtmltopdf/bin/wkhtmltoimage",
    ]
    for p in common_paths:
        if os.path.exists(p):
            return p
    return None


async def generate_command_table_image(data, title="📘 命令一览表"):
    # 查找 wkhtmltoimage，找不到时直接返回 None（调用方会自动降级为文本）
    wkhtmltoimage_path = _find_wkhtmltoimage()
    if wkhtmltoimage_path:
        wkhtml_config = imgkit.config(wkhtmltoimage=wkhtmltoimage_path)
    else:
        logger.debug("未找到 wkhtmltoimage 可执行文件，跳过图片生成（将使用文本降级方案）")
        return None

    # 构造表格行
    rows = ""
    for cmd, usage, example, note in data:
        rows += f"""
        <tr>
            <td>{cmd}</td>
            <td>{usage}</td>
            <td>{example}</td>
            <td>{note}</td>
        </tr>
        """

    # 生成 HTML 字符串
    html_str = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            * {{
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }}
            body {{
                background: #667eea;
                font-family: Arial, sans-serif;
                padding: 8px;
                margin: 0;
            }}
            .container {{
                background: white;
                border-radius: 6px;
                padding: 12px;
                width: 100%;
                max-width: 600px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            }}
            .title {{
                text-align: center;
                color: #333;
                font-size: 18px;
                font-weight: bold;
                margin-bottom: 15px;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                background: white;
                border-radius: 4px;
                font-size: 11px;
                border: 1px solid #ddd;
            }}
            thead {{
                background: #667eea;
            }}
            th {{
                padding: 6px 4px;
                text-align: center;
                color: white;
                font-weight: bold;
                font-size: 10px;
                border: 1px solid #555;
            }}
            tbody tr:nth-child(even) {{
                background: #f8f9fa;
            }}
            td {{
                padding: 4px;
                text-align: center;
                color: #333;
                border: 1px solid #ddd;
                font-size: 9px;
                line-height: 1.2;
            }}
            td:first-child, th:first-child {{
                white-space: nowrap;
                font-weight: bold;
                color: #667eea;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="title">📘 {title}</div>
            <table>
                <thead>
                    <tr>
                        <th>⚡ 命令</th>
                        <th>🎯 作用</th>
                        <th>💡 举例</th>
                        <th>📝 说明</th>
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
    
    # 写入临时 HTML 文件
    #  
    unique_id = uuid.uuid4().hex
    html_file = Path(f"temp_file/command_temp_{unique_id}.html")
    img_file = Path(f"temp_file/command_table_{unique_id}.png")
    html_file.parent.mkdir(parents=True, exist_ok=True)

    with open(html_file, "w", encoding="utf-8") as f:
        f.write(html_str)

        
    options = {
        'encoding': "UTF-8",
        'format': 'png',
        'width': 600,
        'enable-local-file-access': '',
        'quiet': '',
        'quality': 75,
        'disable-smart-width': ''
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