"""通知富文本的安全过滤、表格构建和纯文本降级。"""
from __future__ import annotations

import html
import re
from collections.abc import Iterable, Sequence
from typing import Any

from bs4 import BeautifulSoup, Comment, NavigableString


_ALLOWED_TAGS = {
    "table", "caption", "tr", "th", "td",
    "b", "strong", "i", "em", "mark", "br", "p", "div", "code", "pre",
    "section", "article", "blockquote", "ul", "ol", "li",
    "h1", "h2", "h3", "h4", "h5", "h6",
}
_DANGEROUS_TAGS = {"script", "style", "iframe", "object", "embed", "svg", "math"}
_ALIGN_VALUES = {"left", "center", "right"}
_VALIGN_VALUES = {"top", "middle", "bottom"}


def _positive_span(value: Any) -> str | None:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return str(parsed) if 1 <= parsed <= 100 else None


def sanitize_rich_html(content: str) -> str:
    """只保留 Telegram Rich Message 支持且适合通知使用的安全标签。"""
    soup = BeautifulSoup(str(content or ""), "html.parser")
    for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()

    for tag in list(soup.find_all(True)):
        if not tag.name:
            continue
        name = tag.name.lower()
        if name in _DANGEROUS_TAGS:
            tag.decompose()
            continue
        if name not in _ALLOWED_TAGS:
            tag.unwrap()
            continue

        clean_attrs: dict[str, str] = {}
        if name == "table":
            for attr in ("bordered", "striped"):
                if attr in tag.attrs:
                    clean_attrs[attr] = ""
        if name in {"th", "td"}:
            align = str(tag.attrs.get("align") or "").lower()
            valign = str(tag.attrs.get("valign") or "").lower()
            if align in _ALIGN_VALUES:
                clean_attrs["align"] = align
            if valign in _VALIGN_VALUES:
                clean_attrs["valign"] = valign
            for attr in ("colspan", "rowspan"):
                span = _positive_span(tag.attrs.get(attr))
                if span:
                    clean_attrs[attr] = span
        tag.attrs = clean_attrs

    return "".join(str(node) for node in soup.contents).strip()


def _cell_text(cell: Any) -> str:
    return re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).strip()


def _table_to_plain(table: Any) -> str:
    caption_node = table.find("caption")
    caption = _cell_text(caption_node) if caption_node else ""
    rows = []
    for row in table.find_all("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if cells:
            rows.append((any(cell.name == "th" for cell in cells), [_cell_text(cell) for cell in cells]))

    lines = [f"▌ {caption}"] if caption else []
    if not rows:
        return "\n".join(lines)

    first_is_header, first_cells = rows[0]
    data_rows = rows[1:] if first_is_header else rows
    if first_is_header and data_rows:
        for _, values in data_rows:
            pairs = list(zip(first_cells, values))
            for index, (label, value) in enumerate(pairs):
                prefix = "● " if index == 0 else "  "
                lines.append(f"{prefix}{label}：{value}")
            if len(values) > len(first_cells):
                lines.append("  " + " ｜ ".join(values[len(first_cells):]))
            lines.append("")
    else:
        for _, values in data_rows:
            lines.append("• " + " ｜ ".join(values))

    return "\n".join(lines).rstrip()


def rich_html_to_plain(content: str) -> str:
    """把富文本表格转换成普通账号、微信和 Bark 也清晰可读的文本。"""
    soup = BeautifulSoup(sanitize_rich_html(content), "html.parser")
    for table in list(soup.find_all("table")):
        table.replace_with(NavigableString(f"\n{_table_to_plain(table)}\n"))
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for item in soup.find_all("li"):
        item.insert_before("• ")
        item.append("\n")
    for block in soup.find_all([
        "p", "div", "section", "article", "pre", "blockquote",
        "h1", "h2", "h3", "h4", "h5", "h6",
    ]):
        block.append("\n")
    text = soup.get_text()
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def text_to_rich_html(text: str) -> str:
    """把普通通知正文安全地转换为 Rich Message HTML。"""
    return html.escape(str(text or "")).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def structured_to_rich_html(data: dict[Any, Any] | list[Any]) -> str:
    """把插件直接提交的 dict/list 转换成通知表格。"""
    if isinstance(data, dict):
        if not data:
            raise ValueError("通知内容不能为空")
        if all(isinstance(value, dict) for value in data.values()):
            columns: list[Any] = []
            for value in data.values():
                for key in value:
                    if key not in columns:
                        columns.append(key)
            if columns:
                return build_rich_table(
                    ["项目", *columns],
                    [[key, *(value.get(column, "") for column in columns)] for key, value in data.items()],
                    caption="通知明细",
                )
        return build_rich_table(
            ["项目", "内容"], [[key, value] for key, value in data.items()],
            caption="通知明细",
        )

    if not isinstance(data, list):
        raise TypeError("结构化通知只支持 dict 或 list")
    if not data:
        raise ValueError("通知内容不能为空")
    if all(isinstance(item, dict) for item in data):
        columns: list[Any] = []
        for item in data:
            for key in item:
                if key not in columns:
                    columns.append(key)
        if not columns:
            raise ValueError("通知内容不能为空")
        return build_rich_table(
            columns, [[item.get(column, "") for column in columns] for item in data],
            caption="通知明细",
        )
    if all(isinstance(item, (list, tuple)) for item in data):
        width = len(data[0])
        if width < 1 or any(len(item) != width for item in data):
            raise ValueError("二维列表的每一行列数必须一致")
        return build_rich_table(
            [f"第 {index + 1} 列" for index in range(width)], data,
            caption="通知明细",
        )
    if any(isinstance(item, (dict, list, tuple)) for item in data):
        raise ValueError("列表内容必须使用同一种结构")
    return build_rich_table(
        ["序号", "内容"], [[index, value] for index, value in enumerate(data, 1)],
        caption="通知明细", align=["right", "left"],
    )


def text_to_notification_rich_html(text: str) -> str:
    """识别常见的多行通知明细，让旧插件的普通文本也能自动显示为表格。"""
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in raw.split("\n") if line.strip()]
    if len(lines) < 2:
        return text_to_rich_html(raw)

    patterns = [
        (
            re.compile(r"^(?:账号|账户)\s*[：:]?\s*(\S+)\s+(.+)$"),
            ["账号", "结果"],
            "账号明细",
            ["left", "left"],
        ),
        (
            re.compile(r"^[\[【]([^\]】]+)[\]】]\s*(.+)$"),
            ["项目", "结果"],
            "处理明细",
            ["left", "left"],
        ),
        (
            re.compile(r"^[•·\-*]?\s*(.+?)\s*\*\s*(\d+)\s*$"),
            ["奖品", "数量"],
            "奖品明细",
            ["left", "right"],
        ),
        (
            re.compile(r"^([^：:]{1,32})[：:]\s*(.+)$"),
            ["项目", "内容"],
            "任务明细",
            ["left", "left"],
        ),
    ]
    for pattern, headers, caption, aligns in patterns:
        rows: list[list[str]] = []
        indexes: list[int] = []
        for index, line in enumerate(lines):
            match = pattern.match(line)
            if match:
                rows.append([match.group(1).strip(), match.group(2).strip()])
                indexes.append(index)
        if len(rows) < 2:
            continue
        if indexes != list(range(indexes[0], indexes[-1] + 1)):
            continue

        table = build_rich_table(
            headers,
            rows,
            caption=caption,
            bordered=True,
            striped=True,
            align=aligns,
        )
        parts = []
        if indexes[0] > 0:
            parts.append(text_to_rich_html(chr(10).join(lines[:indexes[0]])))
        parts.append(table)
        if indexes[-1] + 1 < len(lines):
            parts.append(text_to_rich_html(chr(10).join(lines[indexes[-1] + 1:])))
        return "<br><br>".join(parts)

    # “今日回复 3，签到 已签到”这类单行统计拆成项目表格，后续字段行一并收纳。
    for index, line in enumerate(lines):
        segments = [value.strip() for value in re.split(r"[，,]", line) if value.strip()]
        metric_rows = []
        for segment in segments:
            match = re.match(r"^(.{1,12}?)[：:\s]+([^：:]+)$", segment)
            if not match:
                metric_rows = []
                break
            metric_rows.append([match.group(1).strip(), match.group(2).strip()])
        if len(metric_rows) < 2:
            continue

        end_index = index
        for following in lines[index + 1:]:
            match = re.match(r"^([^：:]{1,32})[：:]\s*(.+)$", following)
            if not match:
                break
            metric_rows.append([match.group(1).strip(), match.group(2).strip()])
            end_index += 1
        table = build_rich_table(
            ["项目", "结果"], metric_rows, caption="运行结果",
            bordered=True, striped=True, align=["left", "left"],
        )
        parts = []
        if index > 0:
            parts.append(text_to_rich_html(chr(10).join(lines[:index])))
        parts.append(table)
        if end_index + 1 < len(lines):
            parts.append(text_to_rich_html(chr(10).join(lines[end_index + 1:])))
        return "<br><br>".join(parts)

    # 抽奖等待等通知常由“状态 + UUID + 消息链接”组成，转换为简单信息表。
    uuid_indexes = [
        index for index, line in enumerate(lines)
        if re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", line)
    ]
    url_indexes = [index for index, line in enumerate(lines) if re.fullmatch(r"https?://\S+", line)]
    if len(uuid_indexes) == 1 and len(url_indexes) == 1:
        uuid_index = uuid_indexes[0]
        url_index = url_indexes[0]
        other_lines = [
            line for index, line in enumerate(lines)
            if index not in {uuid_index, url_index}
        ]
        if len(other_lines) == 1:
            return build_rich_table(
                ["项目", "内容"],
                [["状态", other_lines[0]], ["抽奖编号", lines[uuid_index]], ["查看消息", lines[url_index]]],
                caption="抽奖信息", bordered=True, striped=True, align=["left", "left"],
            )

    return text_to_rich_html(raw)


def _normalise_rows(rows: Iterable[Sequence[Any]]) -> list[list[str]]:
    values: list[list[str]] = []
    for row in rows:
        if isinstance(row, (str, bytes)) or not isinstance(row, Sequence):
            raise TypeError("表格的每一行都必须是列表或元组")
        values.append([str(cell if cell is not None else "") for cell in row])
    return values


def build_rich_table(
    headers: Sequence[Any],
    rows: Iterable[Sequence[Any]],
    *,
    caption: str | None = None,
    bordered: bool = True,
    striped: bool = True,
    align: str | Sequence[str] = "left",
    valign: str = "middle",
) -> str:
    """从结构化数据生成安全的 Rich Message 表格。"""
    if isinstance(headers, (str, bytes)) or not isinstance(headers, Sequence) or not headers:
        raise ValueError("表格至少需要一个表头")
    header_values = [str(value if value is not None else "") for value in headers]
    row_values = _normalise_rows(rows)
    if any(len(row) != len(header_values) for row in row_values):
        raise ValueError("表格每一行的列数必须与表头一致")

    if isinstance(align, str):
        aligns = [align.lower()] * len(header_values)
    else:
        aligns = [str(value).lower() for value in align]
        if len(aligns) != len(header_values):
            raise ValueError("对齐方式数量必须与表格列数一致")
    if any(value not in _ALIGN_VALUES for value in aligns):
        raise ValueError("水平对齐只支持 left、center 或 right")
    valign = str(valign).lower()
    if valign not in _VALIGN_VALUES:
        raise ValueError("垂直对齐只支持 top、middle 或 bottom")

    attrs = ""
    if bordered:
        attrs += " bordered"
    if striped:
        attrs += " striped"
    parts = [f"<table{attrs}>"]
    if caption:
        parts.append(f"<caption>{html.escape(str(caption))}</caption>")
    parts.append("<tr>")
    for value, column_align in zip(header_values, aligns):
        parts.append(
            f'<th align="{column_align}" valign="{valign}">{html.escape(value)}</th>'
        )
    parts.append("</tr>")
    for row in row_values:
        parts.append("<tr>")
        for value, column_align in zip(row, aligns):
            parts.append(
                f'<td align="{column_align}" valign="{valign}">{html.escape(value)}</td>'
            )
        parts.append("</tr>")
    parts.append("</table>")
    return "".join(parts)
