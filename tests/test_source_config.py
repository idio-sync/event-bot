from app import config
from app.sources import THEATERS, fetchers
import pytest


def test_warehouse_registered():
    settings = config.Settings(theaters=("warehouse-rotunda",))
    assert "warehouse-rotunda" in config.ALL_THEATERS
    assert THEATERS["warehouse-rotunda"].short == "Warehouse"
    assert callable(fetchers(settings)["warehouse-rotunda"])


def test_new_source_settings(monkeypatch):
    monkeypatch.setenv("PARKWAY_EVENTIVE_BUCKET", "custom-bucket")
    monkeypatch.setenv("CINEMARK_DAYS_AHEAD", "450")
    settings = config.load()
    assert settings.parkway_eventive_bucket == "custom-bucket"
    assert settings.cinemark_days_ahead == 450


def test_unknown_theater_is_rejected(monkeypatch):
    monkeypatch.setenv("THEATERS", "charles,warehouse-typo")
    with pytest.raises(ValueError, match="warehouse-typo"):
        config.load()


def test_parkway_fetcher_available_without_manual_key():
    settings = config.Settings(theaters=("parkway",))
    assert callable(fetchers(settings)["parkway"])
