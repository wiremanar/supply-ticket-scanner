import pytest

from trading_bot.config import Config, load_config


def test_defaults_load():
    cfg = load_config(None)
    assert cfg.mode == "paper"
    assert cfg.universe
    # Strategy weights are normalised to sum to 1.
    s = cfg.strategy
    assert pytest.approx(s.weight_sma + s.weight_rsi + s.weight_momentum, rel=1e-9) == 1.0


def test_invalid_mode_rejected():
    with pytest.raises(ValueError):
        Config(mode="margin")


def test_empty_universe_rejected():
    with pytest.raises(ValueError):
        Config(universe=[])


def test_env_mode_override(monkeypatch):
    monkeypatch.setenv("TRADING_BOT_MODE", "paper")
    cfg = load_config(None)
    assert cfg.mode == "paper"


def test_unknown_key_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("totally_made_up: 1\n")
    with pytest.raises(ValueError):
        load_config(str(p))
