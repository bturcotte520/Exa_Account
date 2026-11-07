"""Synthetic test: local setup/import never requires credentials or Exa calls."""
import json
import subprocess
import sys
from pathlib import Path

from app.config import load_settings
from app.store import Store
from app.replay import sanitize_export
from app.updates import normalized_url

ROOT = Path(__file__).resolve().parents[1]


def test_signal_sources_share_discovery_url_normalization():
    assert normalized_url("http://www.example.com/news/?utm_source=test#section") == normalized_url("https://example.com/news")
    assert normalized_url("javascript:alert(1)") is None


def test_recording_omits_embedded_pages_but_keeps_request_options():
    request = {"request": {"method": "POST", "body": {
        "query": 'Research example.com <evidence>{"text":"page </evidence> more copyrighted text"}</evidence>',
        "contents": {"text": True, "highlights": True},
    }}}
    cleaned = sanitize_export(request)
    query = cleaned["request"]["body"]["query"]
    assert "Research example.com" in query
    assert "copyrighted" not in query
    assert cleaned["request"]["body"]["contents"] == {"text": True, "highlights": True}


def test_explicit_environment_file_is_preserved(tmp_path, monkeypatch):
    monkeypatch.delenv("exa_key", raising=False)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    env = tmp_path / "original.env"
    original = "exa_key=synthetic-only\njev_key=unused\nfin_data=unused-too\n"
    env.write_text(original)
    settings = load_settings(env)
    assert settings.api_key == "synthetic-only"
    assert env.read_text() == original


def test_cli_import_is_isolated_and_rejects_pending_example(tmp_path):
    source = tmp_path / "synthetic.json"
    source.write_text(json.dumps({
        "format": "account-watch", "version": 1, "synthetic": True,
        "recorded_at": "2026-11-06T00:00:00Z",
        "records": {"accounts": [{"id": "synthetic-account", "name": "Synthetic test"}]},
    }))
    database = tmp_path / "import.sqlite3"
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/import_recording.py"), str(source), "--db", str(database)],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    store = Store(database)
    assert store.list("accounts") == []
    recordings = store.list("recordings")
    assert len(recordings) == 1
    assert recordings[0]["label"] == "Synthetic test"
    rejected = subprocess.run(
        [sys.executable, str(ROOT / "tools/import_recording.py"),
         str(ROOT / "examples/recorded-session/status.json"), "--db", str(database)],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert rejected.returncode == 1
    assert len(store.list("recordings")) == 1


def test_start_help_has_explicit_env_and_services():
    result = subprocess.run([sys.executable, str(ROOT / "tools/start.py"), "--help"],
                            capture_output=True, text=True)
    assert result.returncode == 0
    assert "--env-file" in result.stdout
    assert "ui,receiver" in result.stdout
