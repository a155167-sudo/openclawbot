# 南京文字記餐第二 checkpoint

- 基線：`feature/nanjing-meal-logging`，保留第一 checkpoint 的 `server.py` 303-line working-tree diff，不重做、不 commit/push/deploy。
- Ownership：本 checkpoint 是唯一 `server.py` writer；父控已完成並擁有 `nutrition_reference.py`、`nutrition_estimate_checks.py`、`nutrition_estimate_audit.py` 與參考 JSON，本 checkpoint 僅在必要整合缺口時調整 helper。
- 隔離：只跑 `tests.conftest` temp DB / fake provider / fake LINE；不連 live AI、Google Sheet、LINE 或 production DB。
- 固定契約：canonical meal slots 仍為早餐/午餐/晚餐/點心，所有「宵夜」入口映射點心；reference 僅精確品名且單位相容，TFDA 資料僅 g，500ml 無糖豆漿不得套 reference 或燕麥豆漿菜單值。
- 實作步驟（逐條 RED→GREEN）：
  1. provider JSON contract 加入 `basis_amount`、`basis_unit` 與 calories/protein/fat/carbohydrate 四素；`validate_total_basis` fail closed。
  2. provider raw content 在 normalization 前以實際 model/operation trace 寫最小化 audit；invalid/error attempt 也 audit，audit 儲存失敗不得回報成功。
  3. deterministic explicit/private/reference 優先於 AI，零 provider/零 quota；一般食物 prompt 不注入整份 `MAIN_DISHES`，只有精確且 quantity/unit 相容的描述可提供。
  4. normalize→draft→confirm→ledger 保存四素、source、portion、trace；未知 F/C 不補 0；不一致草稿不可正常確認，只能修改/取消（AI 最多一次重估且同操作一次額度）。
  5. 草稿卡顯示來源、份量/假設、熱量、蛋白質、脂肪、碳水及確認/修改/取消；所有宵夜文字入口 canonicalize 為點心。
  6. focused tests、compile、diff check；完整 runner 留父控。

## 接手狀態

尚未開始本 checkpoint 產品修改；下一步先新增 focused RED tests，確認失敗原因後逐條最小實作。
