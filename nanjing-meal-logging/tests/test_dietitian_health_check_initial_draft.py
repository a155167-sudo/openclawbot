from __future__ import annotations

import copy
import unittest

from dietitian_health_check_initial_draft import assemble_system_data_summary


BASE_DETAIL = {
    "case_id": "case-fixture",
    "status": "ready_for_review",
    "current_review_version": 0,
    "latest_review": None,
    "latest_review_available": False,
    "latest_review_fresh": None,
    "source_token": "a" * 64,
    "profile": {
        "name": "測試顧客",
        "tdee": None,
        "protein": None,
        "goal": None,
        "restrictions": None,
        "active_days": None,
    },
    "valid_days": [
        {
            "local_date": "2026-09-01",
            "qualifying_meal_count": 2,
            "completeness_status": "qualified",
            "rule_version": "fixture-rule-v1",
        },
        {
            "local_date": "2026-09-02",
            "qualifying_meal_count": 2,
            "completeness_status": "qualified",
            "rule_version": "fixture-rule-v1",
        },
        {
            "local_date": "2026-09-03",
            "qualifying_meal_count": 2,
            "completeness_status": "qualified",
            "rule_version": "fixture-rule-v1",
        },
    ],
    "source_integrity": {
        "referenced_count": 6,
        "available_snapshot_count": 6,
        "all_snapshots_available": True,
    },
    "source_logs": [
        {"log_id": "l1", "local_date": "2026-09-01", "normalized_meal_slot": "breakfast", "nutrition_snapshot": {"calories_kcal": 400, "protein_g": 20}},
        {"log_id": "l2", "local_date": "2026-09-01", "normalized_meal_slot": "dinner", "nutrition_snapshot": {"calories_kcal": 600, "protein_g": 30}},
        {"log_id": "l3", "local_date": "2026-09-02", "normalized_meal_slot": "breakfast", "nutrition_snapshot": {"calories_kcal": 500, "protein_g": 25}},
        {"log_id": "l4", "local_date": "2026-09-02", "normalized_meal_slot": "dinner", "nutrition_snapshot": {"calories_kcal": 700, "protein_g": 35}},
        {"log_id": "l5", "local_date": "2026-09-03", "normalized_meal_slot": "breakfast", "nutrition_snapshot": {"calories_kcal": 300}},
        {"log_id": "l6", "local_date": "2026-09-03", "normalized_meal_slot": "dinner", "nutrition_snapshot": {"calories_kcal": 500}},
    ],
}


class InitialDraftTests(unittest.TestCase):
    def test_builds_read_only_unsaved_summary_from_exact_dto_values(self):
        detail = copy.deepcopy(BASE_DETAIL)
        before = copy.deepcopy(detail)

        result = assemble_system_data_summary(detail)

        self.assertEqual(detail, before)
        self.assertEqual(result["kind"], "system_data_summary_v1")
        self.assertEqual(result["persistence"], "not_saved")
        self.assertFalse(result["customer_visible"])
        self.assertFalse(result["delivery_eligible"])
        self.assertTrue(result["requires_dietitian_review"])
        self.assertEqual(result["source_binding"], {
            "source_token": "a" * 64,
            "review_version": 0,
        })
        self.assertEqual(result["coverage"]["qualified_day_count"], 3)
        self.assertEqual(result["coverage"]["observed_day_count"], 3)
        self.assertEqual(result["coverage"]["source_count"], 6)
        self.assertEqual(result["days"][0], {
            "local_date": "2026-09-01",
            "completeness_status": "qualified",
            "qualifying_meal_count": 2,
            "source_count": 2,
            "nutrition_totals": {"calories_kcal": 1000, "protein_g": 50},
            "partial_nutrition_recorded": {},
        })
        self.assertEqual(result["nutrition"]["calories_kcal"], {
            "recorded_sum": 3000,
            "qualified_recorded_sum": 3000,
            "qualified_day_average": 1000,
            "qualified_days_with_complete_value": 3,
            "partial_days_with_recorded_value": 0,
            "partial_recorded_sum": 0,
            "source_records_with_value": 6,
            "unit": "kcal",
        })
        self.assertEqual(result["nutrition"]["protein_g"], {
            "recorded_sum": 110,
            "qualified_recorded_sum": 110,
            "qualified_day_average": 55,
            "qualified_days_with_complete_value": 2,
            "partial_days_with_recorded_value": 0,
            "partial_recorded_sum": 0,
            "source_records_with_value": 4,
            "unit": "g",
        })
        self.assertIsNone(result["targets"]["calories_kcal"])
        self.assertIsNone(result["targets"]["protein_g"])
        self.assertNotIn("AI", result["display_label"])
        self.assertEqual(result["editable_seed"]["comment"], "")
        self.assertIn("3 個符合完整度規則", result["editable_seed"]["good"])
        self.assertIn("由營養師確認", result["editable_seed"]["priority"])
        self.assertIn("由營養師", result["editable_seed"]["next_7_days"])
        self.assertNotIn("建議每天", " ".join(result["editable_seed"].values()))

    def test_missing_nutrients_and_targets_are_not_coerced_to_zero(self):
        detail = copy.deepcopy(BASE_DETAIL)
        for log in detail["source_logs"]:
            log["nutrition_snapshot"].pop("protein_g", None)

        result = assemble_system_data_summary(detail)

        self.assertNotIn("protein_g", result["nutrition"])
        self.assertIsNone(result["targets"]["protein_g"])
        self.assertTrue(any(text.startswith("蛋白質目標未提供") for text in result["limitations"]))

    def test_existing_review_is_never_replaced_or_reused_as_system_output(self):
        detail = copy.deepcopy(BASE_DETAIL)
        detail.update({
            "current_review_version": 2,
            "latest_review_available": True,
            "latest_review_fresh": True,
            "latest_review": {
                "review_id": "existing-review",
                "review_version": 2,
                "status": "draft",
                "review": {"good": "人工原稿"},
            },
        })
        before = copy.deepcopy(detail)

        result = assemble_system_data_summary(detail)

        self.assertEqual(detail, before)
        self.assertEqual(result, {
            "kind": "existing_review_preserved",
            "reason": "saved_review_exists",
            "review_version": 2,
            "review_status": "draft",
            "may_generate_preview": False,
        })

    def test_approved_or_terminal_case_never_gets_preview(self):
        for status in ("approved_pending_delivery", "delivery_failed", "delivered", "expired", "cancelled"):
            with self.subTest(status=status):
                detail = copy.deepcopy(BASE_DETAIL)
                detail["status"] = status
                result = assemble_system_data_summary(detail)
                self.assertEqual(result["kind"], "preview_unavailable")
                self.assertEqual(result["reason"], "case_not_draft_writable")
                self.assertFalse(result["may_generate_preview"])

    def test_incomplete_or_stale_source_projection_fails_closed(self):
        variants = []
        missing_snapshot = copy.deepcopy(BASE_DETAIL)
        missing_snapshot["source_integrity"]["all_snapshots_available"] = False
        missing_snapshot["source_integrity"]["available_snapshot_count"] = 5
        variants.append(missing_snapshot)

        no_binding = copy.deepcopy(BASE_DETAIL)
        no_binding["source_token"] = ""
        variants.append(no_binding)

        stale_review_signal = copy.deepcopy(BASE_DETAIL)
        stale_review_signal.update({
            "current_review_version": 1,
            "latest_review": None,
            "latest_review_available": False,
            "latest_review_fresh": False,
        })
        variants.append(stale_review_signal)

        for detail in variants:
            result = assemble_system_data_summary(detail)
            self.assertIn(result["kind"], {"preview_unavailable", "existing_review_preserved"})
            self.assertFalse(result["may_generate_preview"])

    def test_rejects_duplicate_sources_bad_numbers_and_day_count_mismatch(self):
        duplicate = copy.deepcopy(BASE_DETAIL)
        duplicate["source_logs"][1]["log_id"] = "l1"
        bad_number = copy.deepcopy(BASE_DETAIL)
        bad_number["source_logs"][0]["nutrition_snapshot"]["calories_kcal"] = float("nan")
        mismatched_count = copy.deepcopy(BASE_DETAIL)
        mismatched_count["source_integrity"]["referenced_count"] = 7

        for detail in (duplicate, bad_number, mismatched_count):
            with self.assertRaises(ValueError):
                assemble_system_data_summary(detail)

    def test_incomplete_recorded_day_is_separate_and_never_creates_negative_missing_days(self):
        detail = copy.deepcopy(BASE_DETAIL)
        detail["valid_days"].append({
            "local_date": "2026-09-04",
            "qualifying_meal_count": 1,
            "completeness_status": "incomplete",
            "rule_version": "fixture-rule-v1",
        })
        detail["source_logs"].append({
            "log_id": "l7",
            "local_date": "2026-09-04",
            "normalized_meal_slot": "breakfast",
            "nutrition_snapshot": {"calories_kcal": 200, "protein_g": 10},
        })
        detail["source_integrity"] = {
            "referenced_count": 7,
            "available_snapshot_count": 7,
            "all_snapshots_available": True,
        }

        result = assemble_system_data_summary(detail)

        self.assertEqual(result["coverage"]["observed_day_count"], 4)
        self.assertEqual(result["coverage"]["qualified_day_count"], 3)
        self.assertEqual(result["days"][-1]["completeness_status"], "incomplete")
        calories = result["nutrition"]["calories_kcal"]
        self.assertEqual(calories["qualified_day_average"], 1000)
        self.assertEqual(calories["qualified_days_with_complete_value"], 3)
        self.assertEqual(calories["partial_days_with_recorded_value"], 1)
        self.assertEqual(calories["partial_recorded_sum"], 200)
        observation = next(text for text in result["observations"] if text.startswith("熱量"))
        self.assertNotIn("-1 天", observation)
        self.assertIn("不納入日平均", observation)

    def test_any_missing_metric_in_a_day_makes_that_metric_partial_not_a_daily_total(self):
        detail = copy.deepcopy(BASE_DETAIL)
        del detail["source_logs"][1]["nutrition_snapshot"]["calories_kcal"]

        result = assemble_system_data_summary(detail)

        calories = result["nutrition"]["calories_kcal"]
        self.assertEqual(calories["qualified_day_average"], 1000)
        self.assertEqual(calories["qualified_days_with_complete_value"], 2)
        self.assertEqual(calories["partial_days_with_recorded_value"], 1)
        self.assertEqual(calories["partial_recorded_sum"], 400)
        self.assertNotIn("calories_kcal", result["days"][0]["nutrition_totals"])
        self.assertEqual(
            result["days"][0]["partial_nutrition_recorded"]["calories_kcal"], 400
        )

    def test_observations_are_descriptive_not_personalized_advice(self):
        result = assemble_system_data_summary(copy.deepcopy(BASE_DETAIL))
        self.assertEqual(result["observations"], [
            "3 個符合完整度規則的日期，共有 6 筆可驗證營養快照。",
            "熱量在 3 個符合記錄門檻且該欄位完整的日期，已記錄量日平均為 1000 kcal。",
            "蛋白質在 2 個符合記錄門檻且該欄位完整的日期，已記錄量日平均為 55 g；1 個門檻日的蛋白質欄位不完整。",
        ])
        self.assertEqual(result["dietitian_tasks"], [
            "判讀飲食型態與優先改善事項",
            "依顧客目標、限制與臨床專業撰寫個人化建議",
            "確認內容後另行儲存版本化草稿；本摘要不會自動送出",
        ])


if __name__ == "__main__":
    unittest.main()
