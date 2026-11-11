import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

import server


def test_no_hardcoded_default_secret_in_source():
    source = Path(server.__file__).read_text(encoding="utf-8")
    assert "#GEN_CODES" not in source
    assert 'os.environ.get("ADMIN_SECRET",' not in source


def test_unset_secret_rejects_everything(monkeypatch):
    monkeypatch.setattr(server, "ADMIN_SECRET", "")
    assert not server.admin_secret_matches("")
    assert not server.admin_secret_matches("#GEN_CODES")
    payload = server.LobsterPayload(admin_secret="", user_id="U1", coach_message="x")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(server.lobster_send_message(payload))
    assert exc.value.status_code == 401


def test_configured_secret_must_match_exactly(monkeypatch):
    monkeypatch.setattr(server, "ADMIN_SECRET", "s3cret-值")
    assert server.admin_secret_matches("s3cret-值")
    assert not server.admin_secret_matches("s3cret")
    assert not server.admin_secret_matches(None)
