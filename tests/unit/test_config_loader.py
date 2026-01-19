import textwrap

from hft_platform.config import loader


def test_load_settings_precedence(tmp_path, monkeypatch):
    base = tmp_path / "config/base/main.yaml"
    env = tmp_path / "config/env/sim/main.yaml"
    settings_py = tmp_path / "config/settings.py"

    base.parent.mkdir(parents=True, exist_ok=True)
    env.parent.mkdir(parents=True, exist_ok=True)

    base.write_text(
        textwrap.dedent(
            """\
            mode: sim
            symbols: ["AAA"]
            strategy:
              id: base
              module: m
              class: C
            """
        )
    )
    env.write_text(
        textwrap.dedent(
            """\
            symbols: ["BBB"]
            strategy:
              id: env
            """
        )
    )
    settings_py.write_text(
        textwrap.dedent(
            """\
            def get_settings():
                return {
                    "symbols": ["CCC"],
                    "strategy": {"id": "settings"},
                }
            """
        )
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(loader, "DEFAULT_YAML_PATH", "config/base/main.yaml")
    monkeypatch.setenv("HFT_MODE", "sim")
    monkeypatch.setenv("HFT_SYMBOLS", "DDD,EEE")

    settings, defaults = loader.load_settings(cli_overrides={"symbols": ["FFF"], "mode": "live"})

    assert defaults["symbols"] == ["AAA"]
    assert settings["symbols"] == ["FFF"]
    assert settings["strategy"]["id"] == "settings"
    assert settings["mode"] == "sim"
