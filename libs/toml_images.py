# 标准库
import os
import uuid
from pathlib import Path

# 第三方库
import imgkit

# 自定义模块
from libs.log import logger

async def toml_file_to_image(toml_file_path: Path):
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
                wkhtml_config = None
    else:
        wkhtml_config = None


    with open(toml_file_path, "r", encoding="utf-8") as f:
        toml_code = f.read()

    # 转义HTML特殊字符
    toml_code = toml_code.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    # 生成唯一文件名
    unique_id = uuid.uuid4().hex
    html_file = Path(f"temp_file/state_temp_{unique_id}.html")
    img_file = Path(f"temp_file/state_table_{unique_id}.png")
    html_file.parent.mkdir(parents=True, exist_ok=True)

    # 使用最简单的HTML结构，避免表格
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
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
                width: 800px;
            }}
            .title {{
                text-align: center;
                color: #333;
                font-size: 18px;
                font-weight: bold;
                margin-bottom: 15px;
            }}
            .content {{
                font-family: monospace;
                font-size: 11px;
                color: #333;
                white-space: pre-wrap;
                background: #f8f9fa;
                padding: 10px;
                border: 1px solid #ddd;
                border-radius: 4px;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="title">配置文件状态</div>
            <div class="content">{toml_code}</div>
        </div>
    </body>
    </html>
    """

    # 写 HTML 文件
    with open(html_file, "w", encoding="utf-8") as f:
        f.write(html_content)


    options = {
        'encoding': "UTF-8",
        'format': 'png',
        'width': 800,
        'enable-local-file-access': '',
        'quiet': ''
    }

    try:
        imgkit.from_file(str(html_file), str(img_file), options=options, config=wkhtml_config)

        # 检查图片文件是否成功生成
        if Path(img_file).exists():
            file_size = Path(img_file).stat().st_size
            logger.info(f"图片生成成功: {img_file}, 大小: {file_size} bytes")
        else:
            logger.error(f"图片文件不存在: {img_file}")
            return None

    except Exception as e:
        logger.error(f"TOML图片生成失败: {e}")
        # 如果图片生成失败，返回None而不是抛出异常
        if Path(html_file).exists():
            Path(html_file).unlink()
        return None

    # 返回图片路径
    Path(html_file).unlink()
    return img_file