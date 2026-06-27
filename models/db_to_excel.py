# 标准库
from pathlib import Path
from datetime import datetime

# 第三方库
from sqlalchemy import select
import pandas as pd

# 自定义模块
from models import async_session_maker


async def export_table_to_file(table_class, file_type='excel'):
    """
    Export the given SQLAlchemy ORM table to a file (CSV or Excel).

    Parameters:
    - table_class: SQLAlchemy ORM class (e.g., User)
    - file_type: 'csv' or 'excel' (default: 'excel')

    Returns:
    - Path object pointing to the exported file
    """
    # 选择文件扩展名
    extension = "xlsx" if file_type == "excel" else "csv"

    # 构造唯一文件名
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_name = f"temp_{table_class.__name__}_{timestamp}.{extension}"
    file_path = Path("temp_file") / file_name

    # 确保输出目录存在
    file_path.parent.mkdir(parents=True, exist_ok=True)

    async with async_session_maker() as session:
        result = await session.execute(select(table_class))
        rows = result.scalars().all()

        # 用模型字段顺序构造 DataFrame
        columns = [c.name for c in table_class.__table__.columns]
        df = pd.DataFrame([{col: getattr(row, col) for col in columns} for row in rows])
        # 根据类型导出文件
        if file_type == 'excel':
            df.to_excel(file_path, index=False)
        elif file_type == 'csv':
            df.to_csv(file_path, index=False)
        else:
            raise ValueError("file_type must be 'csv' or 'excel'.")

    return file_path
