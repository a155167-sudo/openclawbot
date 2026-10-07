"""Create the 包月會員 rich menu (3x2, 2500x1686) for ONE LINE channel.

Usage (run once per channel; staging first):
  LINE_CHANNEL_ACCESS_TOKEN=... python3 ops/create_member_rich_menu.py ops/member_rich_menu.json --dry-run
  LINE_CHANNEL_ACCESS_TOKEN=... python3 ops/create_member_rich_menu.py ops/member_rich_menu.json

It prints the new richMenuId. Put it in Railway as RICH_MENU_MEMBER_ID for the
SAME channel's service. It never changes the channel's default menu; customers
who are not active 包月 members keep the current default menu.
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

WIDTH, HEIGHT, COLS, ROWS = 2500, 1686, 3, 2


def build_payload(config: dict) -> dict:
    tiles = config.get("tiles") or []
    if len(tiles) != COLS * ROWS:
        raise SystemExit(f"需要剛好 {COLS * ROWS} 格，目前 {len(tiles)} 格")
    areas = []
    tile_w, tile_h = WIDTH // COLS, HEIGHT // ROWS
    for index, tile in enumerate(tiles):
        action = dict(tile.get("action") or {})
        if action.get("type") == "uri":
            uri = str(action.get("uri") or "")
            if not uri.startswith("https://") or "請填入" in uri:
                raise SystemExit(f"第 {index + 1} 格「{tile.get('label')}」的連結尚未填好")
        elif action.get("type") == "message":
            if not str(action.get("text") or "").strip():
                raise SystemExit(f"第 {index + 1} 格缺少 text")
        else:
            raise SystemExit(f"第 {index + 1} 格只支援 message 或 uri")
        action["label"] = str(tile.get("label") or "")[:20]
        col, row = index % COLS, index // COLS
        width = WIDTH - tile_w * col if col == COLS - 1 else tile_w
        height = HEIGHT - tile_h * row if row == ROWS - 1 else tile_h
        areas.append({"bounds": {"x": tile_w * col, "y": tile_h * row, "width": width, "height": height},
                      "action": action})
    return {"size": {"width": WIDTH, "height": HEIGHT}, "selected": False,
            "name": str(config.get("name") or "member-menu")[:300],
            "chatBarText": str(config.get("chatBarText") or "選單")[:14], "areas": areas}


def _request(url, token, data, content_type):
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": content_type})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def main(argv):
    if len(argv) < 2:
        raise SystemExit(__doc__)
    config_path = Path(argv[1])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    payload = build_payload(config)
    image = (config_path.parent / config.get("image", "")).resolve()
    if "--dry-run" in argv:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"圖片：{image}（{'存在' if image.exists() else '找不到'}）")
        return
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not token:
        raise SystemExit("請設定 LINE_CHANNEL_ACCESS_TOKEN（要建立選單的那個頻道）")
    if not image.exists():
        raise SystemExit(f"找不到圖片 {image}")
    content_type = "image/png" if image.suffix.lower() == ".png" else "image/jpeg"
    created = json.loads(_request("https://api.line.me/v2/bot/richmenu", token,
                                  json.dumps(payload).encode("utf-8"), "application/json"))
    menu_id = created["richMenuId"]
    _request(f"https://api-data.line.me/v2/bot/richmenu/{menu_id}/content", token,
             image.read_bytes(), content_type)
    print(f"✅ 已建立會員圖文選單：{menu_id}")
    print("請在同一個頻道的 Railway 服務設定 RICH_MENU_MEMBER_ID=" + menu_id)


if __name__ == "__main__":
    main(sys.argv)
