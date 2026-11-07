"""Synthetic test recordings only, never genuine customer evidence."""
import copy
import csv
import io
import json

import pytest

from app.replay import export_csv, export_recording, import_recording, replay_delivery, sanitize_export
from app.models import GEOGRAPHY
from app.store import Store
from app.updates import apply_monitor_update, persist_delivery
from test_updates import setup


def test_replay_twice_isolated_preupdate(setup):
    store, account, monitor, event = setup
    persist_delivery(store, monitor, event)
    apply_monitor_update(store, event["id"])
    before = {kind: store.list(kind) for kind in ("accounts", "signals", "deliveries", "monitors")}
    result = replay_delivery(store, event["id"])
    assert result["before"] == {"accounts": 1, "signals": 0}
    assert result["first"]["added"] == 1
    assert result["second"]["status"] == "duplicate"
    assert result["after_first"] == result["after_second"] == {"accounts": 1, "signals": 1}
    assert before == {kind: store.list(kind) for kind in before}


def test_export_redaction_and_readonly_import(setup):
    store, account, monitor, event = setup
    account.update(employee_count=777, partner_data={"restricted": "secret-partner"})
    store.put("accounts", account)
    store.put("artifacts", {"id": "synthetic-artifact", "response": {"webhookSecret": "signing-secret", "results": [{"url": "https://synthetic.example", "text": "copyright-page", "highlights": ["no-excerpt-rights"]}], "employeeCount": 777}, "request": {"headers": {"x-api-key": "api-secret"}}})
    recording = export_recording(store)
    encoded = json.dumps(recording)
    for secret in ("signing-secret", "copyright-page", "secret-partner", "api-secret", "no-excerpt-rights", "777"):
        assert secret not in encoded
    imported = copy.deepcopy(recording)
    imported["records"]["accounts"][0]["name"] = "Synthetic imported replacement"
    record = import_recording(store, imported)
    assert record["read_only"] is True
    assert store.get("accounts", account["id"])["name"] == "Synthetic Gym"
    assert store.get("monitors", monitor["id"]) == monitor


@pytest.mark.parametrize("bad", [{}, {"format": "other", "version": 1}, {"format": "account-watch", "version": 1, "recorded_at": "now", "records": {"unknown": []}}, {"format": "account-watch", "version": 1, "recorded_at": "now", "records": {"accounts": [{"id": "a"}, {"id": "a"}]}}])
def test_import_validation(tmp_path, bad):
    store = Store(tmp_path / "synthetic.sqlite")
    with pytest.raises(ValueError):
        import_recording(store, bad)
    assert store.list("recordings") == []


def test_csv_one_company_and_formula_protection(setup):
    store, account, _, _ = setup
    account["name"] = "=SYNTHETIC()"
    store.put("accounts", account)
    result = export_csv(store)
    assert len(result.splitlines()) == 2
    assert "'=SYNTHETIC()" in result


def test_partner_provider_payload_omitted():
    assert "restricted" not in json.dumps(sanitize_export({"provider": "fiber", "data": {"unusual": "restricted"}}))


def test_imported_replay_isolated(setup, tmp_path):
    store, _, monitor, event = setup
    persist_delivery(store, monitor, event)
    apply_monitor_update(store, event["id"])
    destination = Store(tmp_path / "import-target.sqlite")
    saved = import_recording(destination, export_recording(store))
    result = replay_delivery(destination, event["id"], saved["id"])
    assert result["first"]["added"] == 1
    assert result["second"]["status"] == "duplicate"
    assert destination.list("accounts") == destination.list("monitors") == []
    assert "claims prior verification" in result["verification_basis"]


def test_export_retains_request_configuration():
    cleaned = sanitize_export({"request": {"body": {"search": {"query": "synthetic query", "contents": {"highlights": True}}}}, "response": {"body": "copyright"}})
    assert cleaned["request"]["body"]["search"]["query"] == "synthetic query"
    assert "body" not in cleaned["response"]


def test_synthetic_recording_label_survives_delivery_replay(setup):
    store, _, monitor, event = setup
    persist_delivery(store, monitor, event)
    apply_monitor_update(store, event["id"])
    recording = export_recording(store)
    recording["synthetic"] = True
    saved = import_recording(store, recording)
    assert replay_delivery(store, event["id"], saved["id"])["label"] == "Synthetic test"


def test_exports_preserve_mixed_geographies_without_migration(setup):
    store, account, _, _ = setup
    legacy = {**account, "id": "legacy"}
    legacy.pop("geography")
    store.put("accounts", legacy)
    rows = list(csv.DictReader(io.StringIO(export_csv(store))))
    assert {row["id"]: row["geography"] for row in rows} == {account["id"]: GEOGRAPHY, "legacy": "Texas"}
    recording = export_recording(store)
    accounts = recording["records"]["accounts"]
    assert accounts[0]["geography"] == GEOGRAPHY
    assert "geography" not in accounts[1]
    assert store.get("accounts", "legacy") == legacy
    imported = import_recording(store, recording)
    assert "geography" not in imported["recording"]["records"]["accounts"][1]
