import user_memory_store as ums


def _store(tmp_path, clock=None):
    now = {"t": 1_000_000.0}
    store = ums.PersistentUserMemory(lambda: str(tmp_path / "m.db"), clock=clock or (lambda: now["t"]))
    return store, now


def test_survives_new_instance_like_a_restart(tmp_path):
    store, _ = _store(tmp_path)
    store["U1"] = [{"role": "user", "content": "嗨"}, {"role": "assistant", "content": "你好"}]
    fresh, _ = _store(tmp_path)
    assert fresh.get("U1") == [{"role": "user", "content": "嗨"}, {"role": "assistant", "content": "你好"}]
    assert "U1" in fresh and "U2" not in fresh and fresh.get("U2", []) == []


def test_per_user_caps_messages_and_length(tmp_path):
    store, _ = _store(tmp_path)
    store["U1"] = [{"role": "user", "content": f"{i}" + "x" * 5000} for i in range(40)]
    saved = store["U1"]
    assert len(saved) == ums.MAX_MESSAGES
    assert saved[-1]["content"].startswith("39") and all(len(m["content"]) == ums.MAX_CHARS for m in saved)


def test_expires_after_ttl_and_delete_works(tmp_path):
    store, now = _store(tmp_path)
    store["U1"] = [{"role": "user", "content": "a"}]
    now["t"] += ums.TTL_DAYS * 86400 + 1
    assert "U1" not in store
    store["U2"] = [{"role": "user", "content": "b"}]  # 寫入時順手清掉過期的
    assert list(store) == ["U2"]
    del store["U2"]
    assert store.pop("U2", None) is None


def test_total_users_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(ums, "MAX_USERS", 3)
    store, now = _store(tmp_path)
    for i in range(5):
        now["t"] += 1
        store[f"U{i}"] = [{"role": "user", "content": "x"}]
    assert sorted(store) == ["U2", "U3", "U4"]


def test_bad_items_are_dropped(tmp_path):
    store, _ = _store(tmp_path)
    store["U1"] = ["oops", {"role": "hacker", "content": "x"}, {"role": "user", "content": 5}]
    assert store["U1"] == [{"role": "user", "content": "5"}]
