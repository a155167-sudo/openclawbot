from customer_navigation import build_customer_function_menu_contents


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def test_customer_menu_has_only_currently_supported_customer_entries():
    contents = build_customer_function_menu_contents(health_services_available=True)
    buttons = [node for node in _walk(contents) if node.get("type") == "button"]
    entries = [(button["action"].get("label"), button["action"].get("text")) for button in buttons]

    assert entries == [
        ("今日總覽", "首頁"),
        ("搜尋餐點", "搜尋"),
        ("記一餐", "我要紀錄飲食"),
        ("常吃・食品", "重選常吃"),
        ("我的排餐", "查看菜單"),
        ("健康服務", "健康服務"),
    ]
    first_row = next(
        node for node in contents["body"]["contents"]
        if node.get("type") == "box" and node.get("layout") == "horizontal"
    )
    assert [button["action"]["text"] for button in first_row["contents"]] == ["首頁", "搜尋"]
    assert contents["type"] == "bubble"
    assert contents["body"]["layout"] == "vertical"


def test_customer_menu_keeps_admin_coach_and_unverified_features_out_of_customer_entry():
    contents = build_customer_function_menu_contents(health_services_available=True)
    serialized = str(contents)
    for forbidden in ("我的資料", "教練工作台", "管理工作台", "Intervals", "營養交換份審核", "水分功能"):
        assert forbidden not in serialized


def test_customer_menu_uses_warm_and_vegetable_green_accents():
    contents = build_customer_function_menu_contents(health_services_available=True)
    colors = {node.get("color") for node in _walk(contents) if node.get("color")}
    assert any(color in {"#EAA75B", "#F2B366", "#F4B942"} for color in colors)
    assert any(color in {"#6B8F71", "#5F8065", "#66856A"} for color in colors)
