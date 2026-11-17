import pytest


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path_factory, monkeypatch):
    """Tests never read or write the real ~/.config/agentsorg registry."""
    from agentsorg import config
    d = tmp_path_factory.mktemp("agentsorg-config")
    monkeypatch.setattr(config, "CONFIG_DIR", d)
    monkeypatch.setattr(config, "REGISTRY", d / "companies.json")
    monkeypatch.setattr(config, "POINTER", d / "home")
    from agentsorg import quota
    monkeypatch.setattr(quota, "CACHE", d / "quota.json")
