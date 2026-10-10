import json
import sqlite3

import pytest

import server
from meal_photo_system import build_meal_photo_estimate_bubble, get_meal_photo_draft
from tests.test_photo_ingredient_controls import _action, _postback, _text
from tests.test_photo_natural_ingredient_batch import _estimate, _setup


def _enter(draft):
    card = build_meal_photo_estimate_bubble(draft)
    server.handle_postback_event(
        _postback(_action(card, "➕ 新增食材")["data"], "REMEDIATION-ENTER")
    )


def _insert_catalog(
    conn,
    *,
    food_id,
    name="青菜",
    owner="U1",
    package_amount=100,
    package_unit="g",
    servings=1,
    calories=50,
    protein=5,
    updated_at="2026-01-01",
):
    server.ensure_daily_food_ledger_schema(conn)
    conn.execute(
        """INSERT INTO food_catalog
           (food_id,product_name,source_type,owner_user_id,visibility,
            package_amount,package_unit,servings_per_package,per_serving_json,
            fingerprint,verification_status,created_at,updated_at)
           VALUES (?,?,'user_private_food',?,'private',?,?,?,?,?,
                   'user_confirmed','2026-01-01',?)""",
        (
            food_id,
            name,
            owner,
            package_amount,
            package_unit,
            servings,
            json.dumps({"calories_kcal": calories, "protein_g": protein}),
            f"fp-{food_id}",
            updated_at,
        ),
    )


@pytest.mark.parametrize(
    "text",
    ["請問青菜20g", "不用青菜20g", "沒有青菜20g", "不想加青菜20g"],
)
def test_real_handler_rejects_question_and_negation_before_all_side_effects(
    tmp_path, monkeypatch, text
):
    db, token, draft, replies = _setup(tmp_path, monkeypatch)
    provider_calls = []
    quota_calls = []
    catalog_calls = []
    real_catalog = server._owner_private_catalog_nutrition
    monkeypatch.setattr(
        server,
        "estimate_text_meal_nutrition",
        lambda request: provider_calls.append(request) or _estimate(request),
    )
    monkeypatch.setattr(
        server,
        "check_permission_and_quota",
        lambda uid: quota_calls.append(uid) or (True, "left"),
    )

    def catalog_spy(*args, **kwargs):
        catalog_calls.append(kwargs)
        return real_catalog(*args, **kwargs)

    monkeypatch.setattr(server, "_owner_private_catalog_nutrition", catalog_spy)
    _enter(draft)
    before = get_meal_photo_draft(sqlite3.connect(db), user_id="U1", token=token)

    server.handle_message(_text(text, "REMEDIATION-NEG-" + str(abs(hash(text)))))

    after = get_meal_photo_draft(sqlite3.connect(db), user_id="U1", token=token)
    assert provider_calls == []
    assert quota_calls == []
    assert catalog_calls == []
    assert (after["status"], after["version"]) == (
        "awaiting_item_name",
        before["version"],
    )
    assert "原草稿未變" in replies[-1].text
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM pending_text_meal_estimates").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM meal_photo_events WHERE action='batch_upsert_items'"
        ).fetchone()[0] == 0


def test_real_handler_keeps_common_food_names_and_small_positive_amounts(
    tmp_path, monkeypatch
):
    _db, _token, draft, _replies = _setup(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        server,
        "estimate_text_meal_nutrition",
        lambda request: calls.append(dict(request)) or _estimate(request),
    )
    _enter(draft)
    server.handle_message(_text("木耳0.5g、青菜20g", "REMEDIATION-POSITIVE"))
    assert [(x["food_name"], x["amount"], x["unit"]) for x in calls] == [
        ("木耳", 0.5, "g"),
        ("青菜", 20.0, "g"),
    ]


@pytest.mark.parametrize("separator", ["、", "，", ",", "\n", "\r\n"])
def test_real_handler_splits_each_supported_separator_before_item_normalization(
    tmp_path, monkeypatch, separator
):
    _db, _token, draft, _replies = _setup(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        server,
        "estimate_text_meal_nutrition",
        lambda request: calls.append(dict(request)) or _estimate(request),
    )
    _enter(draft)
    server.handle_message(
        _text(f"木耳 20 g{separator} 青菜 20 公克", "REMEDIATION-SEP-" + str(ord(separator[0])))
    )
    assert [x["food_name"] for x in calls] == ["木耳", "青菜"]


def test_whitespace_without_a_supported_separator_rejects_residual_second_quantity(
    tmp_path, monkeypatch
):
    db, token, draft, replies = _setup(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        server,
        "estimate_text_meal_nutrition",
        lambda request: calls.append(dict(request)) or _estimate(request),
    )
    _enter(draft)
    before = get_meal_photo_draft(sqlite3.connect(db), user_id="U1", token=token)
    server.handle_message(_text("木耳20g 青菜20g", "REMEDIATION-WHITESPACE"))
    after = get_meal_photo_draft(sqlite3.connect(db), user_id="U1", token=token)
    assert calls == []
    assert (after["status"], after["version"]) == (
        "awaiting_item_name",
        before["version"],
    )
    assert "原草稿未變" in replies[-1].text


def test_owner_private_catalog_scales_100g_serving_to_requested_20g(tmp_path, monkeypatch):
    db, _token, draft, _replies = _setup(tmp_path, monkeypatch)
    provider_calls = []
    monkeypatch.setattr(
        server,
        "estimate_text_meal_nutrition",
        lambda request: provider_calls.append(request) or _estimate(request),
    )
    with sqlite3.connect(db) as conn:
        _insert_catalog(conn, food_id="scale-20g")
        conn.commit()
    _enter(draft)
    server.handle_message(_text("青菜20g", "REMEDIATION-CATALOG-SCALE"))
    with sqlite3.connect(db) as conn:
        state = get_meal_photo_draft(conn, user_id="U1", token=draft["token"])
    item = next(x for x in state["estimate"]["estimate_items"] if x["name"] == "青菜")
    assert provider_calls == []
    assert item["portion"] == "20g"
    assert item["calories_kcal"] == pytest.approx(10)
    assert item["protein_g"] == pytest.approx(1)
    assert item["calories_kcal_range"] == {"min": pytest.approx(10), "max": pytest.approx(10)}
    assert item["protein_g_range"] == {"min": pytest.approx(1), "max": pytest.approx(1)}
    assert item["nutrition_source"] == "owner_private_catalog"


@pytest.mark.parametrize(
    "request_amount,request_unit,package_amount,package_unit,servings,expected",
    [
        (20, "g", 0.1, "kg", 1, 0.2),
        (20, "g", 100, "公克", 1, 0.2),
        (20, "g", 100, "克", 1, 0.2),
        (20, "ml", 100, "毫升", 1, 0.2),
        (1 / 6, "piece", 1, "顆", 1, 1 / 6),
        (2, "serving", 100, "g", 4, 2),
    ],
)
def test_catalog_scaling_supports_only_valid_explicit_compatible_bases(
    tmp_path,
    request_amount,
    request_unit,
    package_amount,
    package_unit,
    servings,
    expected,
):
    db = tmp_path / "catalog-basis.db"
    with sqlite3.connect(db) as conn:
        _insert_catalog(
            conn,
            food_id="basis",
            package_amount=package_amount,
            package_unit=package_unit,
            servings=servings,
            calories=50,
            protein=0,
        )
        result = server._owner_private_catalog_nutrition(
            conn,
            user_id="U1",
            food_name="青菜",
            amount=request_amount,
            unit=request_unit,
        )
    assert result["calories_kcal"] == pytest.approx(50 * expected)
    assert result["protein_g"] == 0


@pytest.mark.parametrize(
    "package_amount,package_unit,servings,request_unit",
    [
        (None, "g", 1, "g"),
        (100, "g", 0, "g"),
        (100, "g", float("inf"), "g"),
        (100, "ml", 1, "g"),
        (100, "unknown", 1, "g"),
    ],
)
def test_unreliable_or_incompatible_catalog_basis_falls_back_to_ai_draft(
    tmp_path, monkeypatch, package_amount, package_unit, servings, request_unit
):
    db, _token, draft, _replies = _setup(tmp_path, monkeypatch)
    provider_calls = []
    monkeypatch.setattr(
        server,
        "estimate_text_meal_nutrition",
        lambda request: provider_calls.append(dict(request)) or _estimate(request),
    )
    with sqlite3.connect(db) as conn:
        _insert_catalog(
            conn,
            food_id="bad-basis",
            package_amount=package_amount,
            package_unit=package_unit,
            servings=servings,
        )
        conn.commit()
    _enter(draft)
    text = "青菜20g" if request_unit == "g" else "青菜20ml"
    if request_unit == "g":
        server.handle_message(_text(text, "REMEDIATION-AI-FALLBACK-" + str(abs(hash(str((package_amount, package_unit, servings)))))))
        with sqlite3.connect(db) as conn:
            state = get_meal_photo_draft(conn, user_id="U1", token=draft["token"])
        item = next(x for x in state["estimate"]["estimate_items"] if x["name"] == "青菜")
        assert provider_calls == [{"food_name": "青菜", "amount": 20.0, "unit": "g", "meal_slot": ""}]
        assert item["nutrition_source"] == "ai_text_estimate"
    else:
        with sqlite3.connect(db) as conn:
            assert server._owner_private_catalog_nutrition(
                conn, user_id="U1", food_name="青菜", amount=20, unit=request_unit
            ) is None


def test_catalog_exact_match_is_owner_isolated_and_ambiguous_owner_rows_do_not_hard_pick(
    tmp_path,
):
    db = tmp_path / "catalog-owner.db"
    with sqlite3.connect(db) as conn:
        _insert_catalog(conn, food_id="foreign", owner="U2", calories=999, protein=99)
        assert server._owner_private_catalog_nutrition(
            conn, user_id="U1", food_name="青菜", amount=20, unit="g"
        ) is None
        _insert_catalog(conn, food_id="mine-1", calories=50, protein=5)
        _insert_catalog(conn, food_id="mine-2", calories=100, protein=10, updated_at="2026-02-01")
        assert server._owner_private_catalog_nutrition(
            conn, user_id="U1", food_name="青菜", amount=20, unit="g"
        ) is None


def test_four_field_user_provided_nutrition_is_for_the_whole_entered_portion(
    tmp_path, monkeypatch
):
    db, _token, draft, _replies = _setup(tmp_path, monkeypatch)
    provider_calls = []
    monkeypatch.setattr(
        server,
        "estimate_text_meal_nutrition",
        lambda request: provider_calls.append(request) or _estimate(request),
    )
    _enter(draft)
    server.handle_message(_text("人工營養｜20g｜10｜1", "REMEDIATION-USER-PROVIDED"))
    with sqlite3.connect(db) as conn:
        state = get_meal_photo_draft(conn, user_id="U1", token=draft["token"])
    item = next(x for x in state["estimate"]["estimate_items"] if x["name"] == "人工營養")
    assert provider_calls == []
    assert item["portion"] == "20g"
    assert item["calories_kcal"] == 10
    assert item["protein_g"] == 1
    assert item["nutrition_source"] == "user_provided"
