from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


def _compact_detail(
    *,
    period: tuple[str, str, str],
    meals: tuple[tuple[str, str, str], tuple[str, str, str], tuple[str, str, str]],
    metric_label: str,
    metric_value: str,
    limitation: str,
    good: str,
    priority: str,
    action: str,
    comment: str,
) -> dict[str, Any]:
    day_payload = []
    for index, (breakfast, lunch, dinner) in enumerate(meals):
        day_payload.append(
            {
                "label": f"第{index + 1}天・{period[index]}",
                "meals": [
                    {"slot": "早餐", "foods": breakfast, "review": "已由顧客確認", "tone": "dark"},
                    {"slot": "午餐", "foods": lunch, "review": "已由顧客確認", "tone": "alt"},
                    {"slot": "晚餐", "foods": dinner, "review": "已由顧客確認", "tone": "default"},
                ],
            }
        )
    return {
        "period": list(period),
        "height_cm": None,
        "weight_kg": None,
        "conditions": "NA",
        "medications": "NA",
        "days": day_payload,
        "metrics": [
            {"label": metric_label, "value": metric_value, "percent": 60, "tone": "amber"}
        ],
        "limitations": limitation,
        "draft": {"good": good, "priority": priority, "action": action, "comment": comment},
    }


DEMO_CASES: tuple[dict[str, Any], ...] = (
    {
        "case_id": "demo-chen",
        "customer_name": "陳○○",
        "goal": "減脂",
        "status": "pending",
        "waiting_hours": 18,
        "record_days": 3,
        "meal_count": 8,
        "completeness": 82,
        "insight": "早餐蛋白質偏少；晚餐主食沒有明顯過量",
        "detail": {
            "period": ["9/1", "9/3", "9/6"],
            "height_cm": 165,
            "weight_kg": 68,
            "conditions": "無",
            "medications": "NA",
            "days": [
                {"label": "第1天・9/1", "meals": [
                    {"slot": "早餐", "foods": "無糖豆漿、飯糰", "review": "已由顧客確認", "tone": "default"},
                    {"slot": "午餐", "foods": "雞腿、白飯、高麗菜、滷蛋", "review": "醬汁攝取量不確定", "tone": "alt"},
                    {"slot": "晚餐", "foods": "牛肉麵、小菜", "review": "已由顧客確認", "tone": "dark"},
                ]},
                {"label": "第2天・9/3", "meals": [
                    {"slot": "早餐", "foods": "吐司、黑咖啡", "review": "未看到明確蛋白質來源", "tone": "dark"},
                    {"slot": "午餐", "foods": "雞胸、地瓜、花椰菜", "review": "已由顧客確認", "tone": "alt"},
                    {"slot": "晚餐", "foods": "火鍋肉片、蔬菜、冬粉", "review": "沾醬份量不確定", "tone": "default"},
                ]},
                {"label": "第3天・9/6", "meals": [
                    {"slot": "早餐", "foods": "未記錄", "review": "不能視為沒有吃", "tone": "dark"},
                    {"slot": "午餐", "foods": "自助餐：魚、白飯、青菜", "review": "已由顧客確認", "tone": "default"},
                    {"slot": "晚餐", "foods": "鮭魚、糙米、蔬菜", "review": "已由顧客確認", "tone": "alt"},
                ]},
            ],
            "metrics": [
                {"label": "早餐蛋白質", "value": "1/3天", "percent": 33, "tone": "amber"},
                {"label": "主餐蛋白質", "value": "6/8餐", "percent": 75, "tone": "green"},
                {"label": "蔬菜出現", "value": "5/8餐", "percent": 62, "tone": "amber"},
                {"label": "含糖飲料", "value": "1次", "percent": 18, "tone": "amber"},
            ],
            "limitations": "第3天早餐未記錄；2餐醬汁攝取量不確定。水果可能未拍攝，因此不可直接判定完全沒有食用，也不應把目前熱量估算視為完整每日攝取。",
            "draft": {
                "good": "午餐與晚餐大多有穩定蛋白質來源，主食份量沒有看到明顯過量。",
                "priority": "早餐蛋白質出現頻率較低。比起先取消晚餐澱粉，更建議優先改善早餐內容。",
                "action": "每天早餐增加一份蛋白質來源，例如蛋、無糖豆漿、鮮奶或無糖優格。",
                "comment": "你的晚餐主食目前不是最主要問題，先把早餐吃完整，通常會比一味減少澱粉更容易持續。",
            },
        },
    },
    {"case_id": "demo-lin", "customer_name": "林○○", "goal": "控制體重", "status": "pending", "waiting_hours": 6, "record_days": 3, "meal_count": 7, "completeness": 76, "insight": "蔬菜頻率偏低；兩餐未確認醬汁", "detail": _compact_detail(period=("9/2", "9/4", "9/7"), meals=(("蛋餅、無糖茶", "排骨便當", "滷味、麵"), ("優格、香蕉", "雞肉餐盒", "水餃、酸辣湯"), ("鮪魚蛋吐司", "自助餐", "豆腐、青菜、白飯")), metric_label="蔬菜出現", metric_value="4/7餐", limitation="兩餐醬汁攝取量不確定；第2天晚餐照片只拍到部分內容。", good="三天都有安排早餐，含糖飲料頻率不高。", priority="主要餐點的蔬菜份量較不穩定。", action="接下來七天先讓午餐至少有一碗蔬菜。", comment="先固定一餐補足蔬菜，比同時要求三餐全部改變更容易持續。")},
    {"case_id": "demo-wang", "customer_name": "王○○", "goal": "吃得更健康", "status": "pending", "waiting_hours": 2, "record_days": 3, "meal_count": 9, "completeness": 91, "insight": "紀錄完整；含糖飲料出現2次", "detail": _compact_detail(period=("9/1", "9/2", "9/5"), meals=(("燕麥、牛奶", "鮭魚餐盒", "咖哩飯"), ("地瓜、豆漿", "牛肉麵", "雞肉沙拉"), ("蛋、吐司", "雞腿便當", "火鍋")), metric_label="含糖飲料", metric_value="2次", limitation="飲料甜度為顧客回想，無法由照片確認實際糖量。", good="三天主要餐點完整，蛋白質來源多樣。", priority="含糖飲料可先從兩次減為一次。", action="接下來七天選一次原本會喝的含糖飲料改成無糖。", comment="不需要一次全部戒掉，先替換一杯就有清楚而可追蹤的進步。")},
    {"case_id": "demo-huang", "customer_name": "黃○○", "goal": "提升運動表現", "status": "more", "waiting_hours": 24, "record_days": 3, "meal_count": 6, "completeness": 61, "insight": "等待補充運動日飲料與點心", "detail": _compact_detail(period=("8/30", "9/2", "9/4"), meals=(("香蕉、咖啡", "雞肉飯", "義大利麵"), ("吐司、蛋", "牛肉餐盒", "魚湯"), ("豆漿、地瓜", "豬肉便當", "粥")), metric_label="運動補給資料", metric_value="待補件", limitation="缺少運動前後飲料與點心，暫時不能評估補給時機。", good="主要餐點都有記錄。", priority="需先補充運動前後進食資料。", action="等待顧客補件後再設定七天目標。", comment="資料補齊前不提供運動補給結論。")},
    {"case_id": "demo-liu", "customer_name": "劉○○", "goal": "減脂", "status": "done", "waiting_hours": 0, "record_days": 3, "meal_count": 8, "completeness": 84, "insight": "已送出：早餐增加蛋白質", "detail": _compact_detail(period=("8/28", "8/30", "9/1"), meals=(("咖啡、吐司", "雞肉餐盒", "湯麵"), ("豆漿、蛋", "魚肉便當", "滷味"), ("優格、水果", "牛肉餐盒", "水餃")), metric_label="早餐蛋白質", metric_value="1/3天", limitation="一餐點心未記錄。", good="午晚餐蛋白質大致穩定。", priority="早餐蛋白質較少。", action="每天早餐增加一份蛋白質。", comment="報告已核准送出。")},
    {"case_id": "demo-tsai", "customer_name": "蔡○○", "goal": "控制體重", "status": "done", "waiting_hours": 0, "record_days": 3, "meal_count": 9, "completeness": 93, "insight": "已送出：午餐增加蔬菜", "detail": _compact_detail(period=("8/29", "8/31", "9/2"), meals=(("飯糰、豆漿", "豬肉便當", "雞肉沙拉"), ("蛋餅、牛奶", "牛肉麵", "鮭魚飯"), ("燕麥、優格", "雞腿便當", "火鍋")), metric_label="午餐蔬菜", metric_value="1/3天", limitation="一餐外食蔬菜份量由顧客回想補充。", good="三餐規律且飲料以無糖為主。", priority="午餐蔬菜份量偏少。", action="午餐固定增加半碗至一碗蔬菜。", comment="報告已核准送出。")},
)


class HealthCheckRepository:
    """Repository strictly scoped to one private prototype directory and filename."""

    DEMO_FILENAME = "dietitian-demo.db"
    ALLOWED_TABLES = {"demo_health_check_cases"}

    def __init__(self, db_path: str | Path, *, safe_root: str | Path):
        raw_root = Path(safe_root).expanduser()
        if raw_root.is_symlink():
            raise ValueError("示範資料庫安全目錄不可為符號連結")
        raw_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            raw_root.chmod(0o700)
        except OSError:
            pass
        self.safe_root = raw_root.resolve()

        candidate = Path(db_path).expanduser()
        if candidate.name != self.DEMO_FILENAME:
            raise ValueError("示範資料庫檔名不符合白名單")
        if candidate.is_symlink():
            raise ValueError("示範資料庫不可為符號連結")
        if candidate.parent.resolve() != self.safe_root:
            raise ValueError("示範資料庫必須位於指定安全目錄")
        self.db_path = candidate

    def _assert_safe_path(self) -> None:
        if self.safe_root.is_symlink() or self.db_path.is_symlink():
            raise ValueError("示範資料庫路徑不可為符號連結")
        if self.db_path.name != self.DEMO_FILENAME:
            raise ValueError("示範資料庫檔名不符合白名單")
        if self.db_path.parent.resolve() != self.safe_root:
            raise ValueError("示範資料庫已離開安全目錄")

    def _connect(self) -> sqlite3.Connection:
        self._assert_safe_path()
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize_demo_data(self) -> None:
        self._assert_safe_path()
        if self.db_path.exists():
            with self._connect() as existing:
                tables = {
                    row[0]
                    for row in existing.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                    if not str(row[0]).startswith("sqlite_")
                }
            foreign_tables = tables - self.ALLOWED_TABLES
            if foreign_tables:
                raise ValueError("拒絕開啟含有非示範資料表的資料庫")
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS demo_health_check_cases (
                    case_id TEXT PRIMARY KEY,
                    customer_name TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'more', 'done')),
                    waiting_hours INTEGER NOT NULL,
                    record_days INTEGER NOT NULL,
                    meal_count INTEGER NOT NULL,
                    completeness INTEGER NOT NULL,
                    insight TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    is_demo INTEGER NOT NULL DEFAULT 1 CHECK (is_demo = 1)
                )
                """
            )
            connection.executemany(
                """
                INSERT OR IGNORE INTO demo_health_check_cases (
                    case_id, customer_name, goal, status, waiting_hours,
                    record_days, meal_count, completeness, insight, detail_json, is_demo
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                [
                    (
                        case["case_id"], case["customer_name"], case["goal"],
                        case["status"], case["waiting_hours"], case["record_days"],
                        case["meal_count"], case["completeness"], case["insight"],
                        json.dumps(case["detail"], ensure_ascii=False),
                    )
                    for case in DEMO_CASES
                ],
            )

    def list_cases(self, status: str) -> list[dict[str, Any]]:
        if status not in {"pending", "more", "done"}:
            raise ValueError("unsupported status")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT case_id, customer_name, goal, status, waiting_hours,
                       record_days, meal_count, completeness, insight, is_demo
                FROM demo_health_check_cases
                WHERE status = ?
                ORDER BY waiting_hours DESC, case_id ASC
                """,
                (status,),
            ).fetchall()
        return [{**dict(row), "is_demo": bool(row["is_demo"])} for row in rows]

    def get_case(self, case_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM demo_health_check_cases WHERE case_id = ?",
                (case_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["is_demo"] = bool(result["is_demo"])
        result["detail"] = json.loads(result.pop("detail_json"))
        return result
