import hashlib
import math
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from linebot.models import BubbleContainer, FlexSendMessage

from weekly_trend import build_weekly_trend_contents, read_weekly_trend


pytestmark = pytest.mark.filterwarnings(
    "ignore::linebot.deprecations.LineBotSdkDeprecatedIn30"
)
TODAY = date(2026, 10, 7)


def _make_db(tmp_path, *, tdee=2000, protein=120):
    path = tmp_path / "trend.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE health_profile(user_id TEXT PRIMARY KEY, tdee, protein);
            CREATE TABLE projected_items(
                user_id TEXT NOT NULL,
                date_iso TEXT NOT NULL,
                calories,
                protein
            );
            """
        )
        conn.execute(
            "INSERT INTO health_profile(user_id,tdee,protein) VALUES(?,?,?)",
            ("U1", tdee, protein),
        )
        conn.execute(
            "INSERT INTO health_profile(user_id,tdee,protein) VALUES(?,?,?)",
            ("OTHER", 9999, 999),
        )
    return path


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _projector(calls, *, assert_readonly=True):
    def project(conn, uid, date_iso):
        calls.append((uid, date_iso))
        if assert_readonly:
            assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("CREATE TABLE forbidden_write(value)")
        rows = conn.execute(
            "SELECT calories,protein FROM projected_items "
            "WHERE user_id=? AND date_iso=? ORDER BY rowid",
            (uid, date_iso),
        ).fetchall()
        return [
            {"nutrition": {"calories_kcal": row[0], "protein_g": row[1]}}
            for row in rows
        ]

    return project


def _insert(path, *rows):
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO projected_items(user_id,date_iso,calories,protein) VALUES(?,?,?,?)",
            rows,
        )


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def test_empty_week_is_oldest_first_and_database_stays_byte_identical(tmp_path):
    db = _make_db(tmp_path)
    before = _sha256(db)
    calls = []

    result = read_weekly_trend(
        db_path=db, user_id="U1", today=TODAY,
        project_items=_projector(calls),
    )

    assert result == {
        "days": [
            {"date": "2026-10-01", "label": "四", "logged": False, "calories_kcal": None, "protein_g": None},
            {"date": "2026-10-02", "label": "五", "logged": False, "calories_kcal": None, "protein_g": None},
            {"date": "2026-10-03", "label": "六", "logged": False, "calories_kcal": None, "protein_g": None},
            {"date": "2026-10-04", "label": "日", "logged": False, "calories_kcal": None, "protein_g": None},
            {"date": "2026-10-05", "label": "一", "logged": False, "calories_kcal": None, "protein_g": None},
            {"date": "2026-10-06", "label": "二", "logged": False, "calories_kcal": None, "protein_g": None},
            {"date": "2026-10-07", "label": "今", "logged": False, "calories_kcal": None, "protein_g": None},
        ],
        "calorie_goal": 2000.0,
        "protein_goal": 120.0,
    }
    assert calls == [("U1", f"2026-10-{day:02d}") for day in range(1, 8)]
    assert _sha256(db) == before


def test_partial_week_sums_finite_values_preserves_real_zero_and_excludes_other_user(tmp_path):
    db = _make_db(tmp_path)
    _insert(
        db,
        ("U1", "2026-10-02", 0, 0),
        ("U1", "2026-10-04", 100.04, 10.04),
        ("U1", "2026-10-04", 50.02, 5.02),
        ("OTHER", "2026-10-04", 8000, 800),
        ("U1", "2026-10-05", None, 7),
        ("U1", "2026-10-05", 20, 3),
    )

    result = read_weekly_trend(
        db_path=str(db), user_id="U1", today=TODAY,
        project_items=_projector([]),
    )
    by_date = {day["date"]: day for day in result["days"]}

    assert by_date["2026-10-02"] == {
        "date": "2026-10-02", "label": "五", "logged": True,
        "calories_kcal": 0.0, "protein_g": 0.0,
    }
    assert by_date["2026-10-04"]["calories_kcal"] == 150.1
    assert by_date["2026-10-04"]["protein_g"] == 15.1
    assert by_date["2026-10-05"]["calories_kcal"] is None
    assert by_date["2026-10-05"]["protein_g"] == 10.0


@pytest.mark.parametrize("unknown", [None, float("nan"), float("inf"), -0.01, "not-a-number"])
def test_invalid_nutrition_is_unknown_not_zero(tmp_path, unknown):
    db = _make_db(tmp_path)

    def project(conn, uid, date_iso):
        return [{"nutrition": {"calories_kcal": unknown, "protein_g": unknown}}] if date_iso == TODAY.isoformat() else []

    today_row = read_weekly_trend(
        db_path=db, user_id="U1", today=TODAY, project_items=project,
    )["days"][-1]

    assert today_row["logged"] is True
    assert today_row["calories_kcal"] is None
    assert today_row["protein_g"] is None


@pytest.mark.parametrize("goal", [None, 0, -1, float("inf"), "nan", "bad"])
def test_invalid_goals_are_unknown(tmp_path, goal):
    db = _make_db(tmp_path, tdee=goal, protein=goal)

    result = read_weekly_trend(
        db_path=db, user_id="U1", today=TODAY, project_items=lambda *_: [],
    )

    assert result["calorie_goal"] is None
    assert result["protein_goal"] is None


@pytest.mark.parametrize("days", [0, -1, 8, 1.5, True])
def test_days_outside_integer_one_week_bounds_are_rejected(tmp_path, days):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="days"):
        read_weekly_trend(
            db_path=db, user_id="U1", today=TODAY,
            project_items=lambda *_: [], days=days,
        )


def test_one_day_bound_calls_only_today(tmp_path):
    db = _make_db(tmp_path)
    calls = []
    result = read_weekly_trend(
        db_path=db, user_id="U1", today=TODAY,
        project_items=_projector(calls), days=1,
    )
    assert calls == [("U1", "2026-10-07")]
    assert result["days"][0]["label"] == "今"


def test_renderer_keeps_unknown_gray_real_zero_known_and_roundtrips_line_sdk():
    days = [
        {"label": "一", "logged": False, "calories_kcal": None, "protein_g": None},
        {"label": "二", "logged": True, "calories_kcal": 0, "protein_g": 0},
        {"label": "三", "logged": True, "calories_kcal": float("nan"), "protein_g": -2},
        {"label": "四", "logged": True, "calories_kcal": 600, "protein_g": 40},
    ]

    payload = build_weekly_trend_contents(
        days, calorie_goal=float("inf"), protein_goal=-1,
    )
    calorie_columns = payload["body"]["contents"][2]["contents"]
    protein_columns = payload["body"]["contents"][5]["contents"]
    calorie_bars = [column["contents"][0]["contents"][0] for column in calorie_columns]
    protein_bars = [column["contents"][0]["contents"][0] for column in protein_columns]

    assert [bar["backgroundColor"] for bar in calorie_bars] == ["#E6E9E7", "#6B8F71", "#E6E9E7", "#6B8F71"]
    assert [bar["backgroundColor"] for bar in protein_bars] == ["#E6E9E7", "#E3B341", "#E6E9E7", "#E3B341"]
    assert calorie_bars[1]["height"] == "2px"
    text = "\n".join(node["text"] for node in _walk(payload) if node.get("type") == "text")
    assert "有紀錄 2 天・平均 300 kcal" in text
    assert "有紀錄 2 天・平均 20 g" in text
    assert "目標" not in text
    assert "沒記錄的日子顯示為灰色" in text
    bubble = BubbleContainer.new_from_json_dict(payload)
    assert FlexSendMessage(alt_text="一週趨勢", contents=bubble).as_json_dict()["contents"] == payload


def test_module_has_no_write_or_external_side_effect_hooks():
    source = (Path(__file__).parents[1] / "weekly_trend.py").read_text(encoding="utf-8")
    forbidden = [
        "ensure_schema", "init_db", ".commit(", "INSERT ", "UPDATE ", "DELETE ",
        "google", "quota", "scheduler", "push_message", "reply_message", "requests",
    ]
    assert not [token for token in forbidden if token.lower() in source.lower()]
