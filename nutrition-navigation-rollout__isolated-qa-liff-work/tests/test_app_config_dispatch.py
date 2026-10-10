import pytest
from app_config import load_settings
from test_app_config import isolated_environment


def test_named_environment_requires_dedicated_printer_export_token():
    environ = isolated_environment()
    environ.pop("NANJING_PRINTER_EXPORT_TOKEN")
    with pytest.raises(ValueError, match="NANJING_PRINTER_EXPORT_TOKEN"):
        load_settings(environ)


def test_printer_export_token_is_dedicated_and_long_enough():
    environ = isolated_environment()
    environ["NANJING_PRINTER_EXPORT_TOKEN"] = "p" * 32
    settings = load_settings(environ)
    assert settings.nanjing_printer_export_token == "p" * 32


@pytest.mark.parametrize("value", ["", "short", "staging-token", "test-private-key", "staging-admin-secret"])
def test_named_environment_rejects_missing_short_or_reused_printer_credentials(value):
    environ = isolated_environment()
    environ["NANJING_PRINTER_EXPORT_TOKEN"] = value
    with pytest.raises(ValueError, match="NANJING_PRINTER_EXPORT_TOKEN"):
        load_settings(environ)
