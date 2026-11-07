"""Synthetic integration checks across the private UI and separate receiver."""
from fastapi.testclient import TestClient

from app.config import Settings
from app.models import GEOGRAPHY
from app.main import create_app
from app.receiver import create_app as create_receiver
from app.store import Store


class NoNetwork:
    async def request(self, *args, **kwargs):
        raise AssertionError("A local read-only action attempted an Exa request")


def test_ui_assets_private_routes_and_receiver_isolation(tmp_path):
    settings = Settings(db_path=tmp_path / "test.sqlite3", secrets_path=tmp_path / "secrets")
    store = Store(settings.db_path)
    with TestClient(create_app(settings, store, NoNetwork())) as ui:
        page = ui.get("/")
        assert page.status_code == 200
        assert GEOGRAPHY in page.text
        assert 'name="color-scheme" content="dark"' in page.text
        assert 'id="criteria-panel" open>' in page.text
        assert 'class="ledger panel" open>' in page.text
        assert 'aria-label="Research workflow"' in page.text
        for path in ("/static/app.js", "/static/styles.css"):
            response = ui.get(path)
            assert response.status_code == 200
            assert len(response.content) > 100
        assert ui.get("/api/state").json()["accounts"] == []
        assert ui.get("/api/config").json()["api_key_configured"] is False
        assert ui.get("/api/config").json()["geography"] == GEOGRAPHY
        assert ui.post("/api/discovery", json={}, headers={"Origin": "https://attacker.invalid"}).status_code == 403
        assert ui.get("/api/state", headers={"Host": "attacker.invalid"}).status_code == 400
    with TestClient(create_receiver(settings, store)) as receiver:
        assert receiver.get("/health").status_code == 200
        for path in ("/", "/api/state", "/api/config", "/docs", "/openapi.json", "/static/app.js"):
            assert receiver.get(path).status_code == 404


def test_open_recording_and_exports_do_not_create_live_accounts(tmp_path):
    settings = Settings(db_path=tmp_path / "test.sqlite3")
    store = Store(settings.db_path)
    recording = {"format": "account-watch", "version": 1, "recorded_at": "2026-11-06T00:00:00Z",
                 "synthetic": True, "records": {"accounts": [{"id": "synthetic", "name": "Synthetic test"}]}}
    with TestClient(create_app(settings, store, NoNetwork())) as ui:
        response = ui.post("/api/recordings/open", json={"recording": recording})
        assert response.status_code == 200
        assert response.json()["read_only"] is True
        assert response.json()["label"] == "Synthetic test"
        assert ui.get("/api/state").json()["accounts"] == []
        assert ui.get("/api/export/json").json()["records"]["accounts"] == []
        assert ui.get("/api/export/csv").status_code == 200
        assert store.list("monitors") == []
