"""包月「指定品項」：客人在表單勾想固定吃的餐點，排餐只從這幾樣輪流排。

規則（Jason 2026-11-06 定案）：
- 選填；沒勾就照原本自動配餐。
- 勾 2 樣以上：依序輪流排（A午B晚 → C午A晚 …），同一天兩餐不重複。
- 只勾 1 樣：每天午餐固定那道，晚餐照原本自動配（不會跟午餐重複）。
- 勾到含飲食禁忌、或菜單已下架的品項：略過，通知客人與管理員；全部不能用就退回自動配餐。
- 有指定品項的客人不強制每週排一個食蔬（輕便當）。
- 價格照實際排到的餐點算。

本模組只做純計算；表單讀取、通知文字在 server.py。
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence

# 表單題目建議標題：「想固定吃的餐點（選填，可複選）」，題型「核取方塊格狀」：
# 列＝蛋白質（順序要跟 GRID_ROWS 一樣），欄＝餐盒類型；品名＝列＋欄，例如「雞肉」×「低碳」＝雞肉低碳。
FORM_KEYWORDS = ("想固定吃的餐點", "想固定吃", "指定品項", "固定吃的餐點")
GRID_ROWS = ("豆腐", "鷹嘴豆", "雞肉", "豬肉", "牛肉", "番牛", "鱸魚", "鮭魚")
GRID_COLUMNS = ("便當", "食蔬", "低碳", "沙拉", "番茄麵", "青蔬麵")
_SPLIT_RE = re.compile(r"[,，、;；\n]+")
_ROW_SUFFIX_RE = re.compile(r"\[([^\[\]]+)\]\s*$")


def _cells(value) -> list[str]:
    """一列的答案（字串、清單或 None）→ 欄名清單。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            out.extend(_cells(item))
        return out
    return [re.sub(r"\s+", "", t) for t in _SPLIT_RE.split(str(value)) if t.strip()]


def extract_form_names(data: Mapping) -> list[str]:
    """從表單 webhook 原始資料取出指定品名，支援三種格式：

    1. 格狀題、Apps Script getResponse()：{題目: [[欄,…] 或 None, …]}，依 GRID_ROWS 順序對應列。
    2. 格狀題、試算表 namedValues：{"題目 [雞肉]": "低碳, 便當", …}。
    3. 一般核取方塊：{題目: ["雞肉低碳", …]} 或逗號字串。
    列數對不上 GRID_ROWS 時不猜，回傳空清單外加一個「表格」標記讓管理員知道。
    """
    names: list[str] = []
    for key, value in data.items():
        label = re.sub(r"\s+", "", str(key))
        if not any(keyword in label for keyword in FORM_KEYWORDS) or not value:
            continue
        row_match = _ROW_SUFFIX_RE.search(str(key))
        if row_match:
            row = re.sub(r"\s+", "", row_match.group(1))
            if row in GRID_COLUMNS:  # 表單把列欄對調（列＝餐盒類型）
                names.extend(cell + row for cell in _cells(value))
            else:
                names.extend(row + col for col in _cells(value))
        elif isinstance(value, (list, tuple)) and any(
            item is None or isinstance(item, (list, tuple)) for item in value
        ):
            if len(value) == len(GRID_ROWS):
                for row, cols in zip(GRID_ROWS, value):
                    names.extend(row + col for col in _cells(cols))
            elif len(value) == len(GRID_COLUMNS):  # 列欄對調：列＝餐盒類型
                for col, rows in zip(GRID_COLUMNS, value):
                    names.extend(row + col for row in _cells(rows))
            else:
                names.append("（表格列數和系統設定不同）")
        else:
            names.extend(parse_fixed_pick_names(",".join(_cells(value))))
    deduped: list[str] = []
    for name in names:
        if name and name not in deduped:
            deduped.append(name)
    return deduped


def parse_fixed_pick_names(answer: str | None) -> list[str]:
    """表單答案 → 品名清單（去空白、去重、保留順序）。"""
    names: list[str] = []
    for token in _SPLIT_RE.split(str(answer or "")):
        name = re.sub(r"\s+", "", token)
        if not name or name in names:
            continue
        if name in {"不指定", "無", "都可以", "沒有"}:
            continue
        names.append(name)
    return names


def resolve_fixed_picks(
    names: Sequence[str], safe_menu: Iterable[Mapping], all_menu: Iterable[Mapping]
) -> tuple[list[Mapping], list[str], list[str]]:
    """回傳 (可用餐點, 含禁忌而略過的品名, 菜單沒有而略過的品名)。

    比對用去掉空白的完整品名；不做模糊比對，避免「雞肉便當」誤配到「雞肉便當加大」之類。
    """
    def key(dish):
        return re.sub(r"\s+", "", str(dish.get("name") or ""))

    safe_by_name = {key(dish): dish for dish in safe_menu}
    known = {key(dish) for dish in all_menu}
    usable, unsafe, unknown = [], [], []
    for name in names:
        if name in safe_by_name:
            usable.append(safe_by_name[name])
        elif name in known:
            unsafe.append(name)
        else:
            unknown.append(name)
    return usable, unsafe, unknown


def apply_fixed_picks(plan_requests: Sequence[tuple], fixed: Sequence[Mapping]) -> list[tuple]:
    """把已排序的排餐 (週, 星期序, 週標籤, 星期, 午餐, 晚餐) 換成指定品項。

    - 2 樣以上：第 i 天午餐 = fixed[2i % n]、晚餐 = fixed[(2i+1) % n]（n≥2 時兩者必不同）。
    - 1 樣：午餐 = 那道；晚餐取原本自動配的兩道中不是那道的一道。
    """
    fixed = list(fixed)
    if not fixed:
        return list(plan_requests)
    n = len(fixed)
    out = []
    for i, (w_num, d_num, w_label, day_name, lunch, dinner) in enumerate(plan_requests):
        if n >= 2:
            new_lunch, new_dinner = fixed[(2 * i) % n], fixed[(2 * i + 1) % n]
        else:
            only = fixed[0]
            new_lunch = only
            new_dinner = dinner if dinner.get("name") != only.get("name") else lunch
            if new_dinner.get("name") == only.get("name"):
                raise ValueError("指定品項當天找不到第二道不同的餐點")
        out.append((w_num, d_num, w_label, day_name, new_lunch, new_dinner))
    return out


def admin_line(usable: Sequence[Mapping], unsafe: Sequence[str], unknown: Sequence[str]) -> str:
    """管理員通知用一行；沒有指定品項回空字串。"""
    if not (usable or unsafe or unknown):
        return ""
    parts = [f"指定品項：{'、'.join(d['name'] for d in usable) or '（都不能用，已改自動配餐）'}"]
    if unsafe:
        parts.append(f"略過（含禁忌）：{'、'.join(unsafe)}")
    if unknown:
        parts.append(f"略過（菜單沒有）：{'、'.join(unknown)}")
    return "\n".join(parts) + "\n"


def customer_lines(usable: Sequence[Mapping], unsafe: Sequence[str], unknown: Sequence[str]) -> str:
    """客人確認訊息用；沒有指定品項回空字串。"""
    if not (usable or unsafe or unknown):
        return ""
    lines = []
    if usable:
        names = "、".join(d["name"] for d in usable)
        if len(usable) == 1:
            lines.append(f"🍱 指定品項：{names}（每天午餐，晚餐由系統搭配）")
        else:
            lines.append(f"🍱 指定品項：{names}（輪流排）")
    skipped = [name for name in list(unsafe) + list(unknown) if not name.startswith("（")]
    if skipped:
        lines.append(f"⚠️ {'、'.join(skipped)} 不符合你的飲食禁忌或目前沒有供應，已略過。")
    if not usable:
        lines.append("指定的餐點都不能安排，這期先由系統自動配餐，有問題請找客服。")
    return "\n".join(lines) + "\n"
