from qq_bot.browser_tools import browser_mcp_config


def install_stub(root, machine="test-pc"):
    entry = root / ".tools" / machine / "playwright" / "node_modules" / "@playwright" / "mcp" / "cli.js"
    entry.parent.mkdir(parents=True)
    entry.touch()
    return entry


def test_missing_optional_plugin_does_not_require_node(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPUTERNAME", "test-pc")
    assert browser_mcp_config(tmp_path, "nonexistent-node") is None


def test_machine_local_browser_configuration(monkeypatch, tmp_path):
    from qq_bot import browser_tools
    monkeypatch.setenv("COMPUTERNAME", "test-pc")
    monkeypatch.setenv("QQ_DSH_BROWSER_ENABLED", "true")
    entry = install_stub(tmp_path)
    monkeypatch.setattr(browser_tools.shutil, "which", lambda _: "C:/runtime/node.exe")
    config = browser_mcp_config(tmp_path, "node")
    assert config["command"] == "C:/runtime/node.exe"
    assert config["args"][0] == str(entry)
    assert config["args"][config["args"].index("--browser") + 1] == "msedge"
    profile = str(tmp_path / ".tools" / "test-pc" / "browser-profile")
    assert config["args"][config["args"].index("--user-data-dir") + 1] == profile
    assert "--headless" not in config["args"]
    assert config["transport"] == "stdio"  # No externally exposed control port.


def test_browser_can_be_disabled_after_installation(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPUTERNAME", "test-pc")
    install_stub(tmp_path)
    monkeypatch.setenv("QQ_DSH_BROWSER_ENABLED", "false")
    assert browser_mcp_config(tmp_path, "nonexistent-node") is None
