# UX 收斂隔離 checkpoint

- 寫入範圍：僅 `/home/win-xi/dietitian-v1-restoration-work`；不碰原 repo 以外環境、LINE、Staging、正式環境，不 commit / deploy。
- 基準 HEAD：`7ff6978b5b610c177cbf66b29557fa3e887fc7fd`。
- 開始時 staged diff：空（SHA-256 `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`）。
- 承接既有未暫存差異：`dietitian_health_check_liff.py`、`tests/dietitian_health_check_liff_behavior.js`、`tests/test_dietitian_health_check_liff.py` 共 `101 insertions / 40 deletions`，另有 untracked `tests/test_dietitian_v1_mobile_restoration.py`。
- 本輪最小範圍：preview 專屬 sticky 操作與具來源依據的限制文案；再收斂技術明細層級、desktop 雙欄獨立捲動及三日綁定正／未綁定負控測試。維持既有 API、CAS、request ID、照片授權與生命週期，不改後端契約。
- 驗證命令：原 repo `.venv/bin/python -m pytest`，每次 `timeout 30s` 且以既有測試 fixture 封閉外部邊界；另跑 `git diff --check`。

## Actual diff / result

- RED 已實際出現：mobile 靜態 gate `2 failed, 3 passed`（缺 preview sticky、仍有「技術資料」）；真 route JS harness `1 failed`（preview 時三個編輯 actions 尚未隱藏）。
- GREEN：focused UI `11 passed in 0.92s`；LIFF/API/CAS/approval/delivery/supplement/protected-photo 回歸 `249 passed in 28.79s`（每次 `timeout 30s`、`PYTHONDONTWRITEBYTECODE=1`、no pytest cache）。`git diff --check` 與 Node harness syntax PASS。
- 執行環境沒有 repo `.venv/bin/python`（命令以 exit 127 證實）；改用現有固定 `python3`，未安裝套件、未連外。
- 相對上一份 frozen visual candidate 的本輪四檔差異：產品 `+28/-10`、JS 行為測試 `+9/-4`、既有 Python shell test `+1/-1`、mobile UX test `+16/-1`；完整 unified delta 為 33,231 bytes，SHA-256 `494d8439fa8caf8b0f4f39371bf8e4c246c2641cfecabc0202c6fa2e680fec54`。
- staged diff 仍為空（SHA-256 `e3b0c442…b855`）；後端 API、draft、approval、protected image 四檔與 frozen candidate 逐檔 `cmp` 皆 unchanged。
- 未完成／未冒充完成：尚未複製 browser harness、建立合法三日日期／餐別 browser fixture 或重拍 preview/mobile/desktop；既有 6 筆 unbound browser fixture 保留為負控，產品沒有猜補日期。這些需在下一個 browser lane 以 synthetic image 明示後重驗 overflow、sticky 及照片放大；本輪不宣稱真使用者、LINE 或部署驗收。
