"""Synthetic test data only: no company or finding is genuine research."""
import asyncio
import copy
import hashlib
import hmac
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.models import GEOGRAPHY
from app.monitors import build_monitor_request, create_account_monitor, reconcile_monitor, save_secret, set_monitor_schedule, trigger_check
from app.receiver import create_app
from app.store import Store
from app.updates import apply_monitor_update, normalize_signals, persist_delivery, verify_webhook


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "synthetic.sqlite")
    account = {"id": "synthetic-account", "name": "Synthetic Gym", "domain": "synthetic.example", "domain_verified": True,
               "geography": GEOGRAPHY, "window_start": "2026-08-01", "window_end": "2026-11-06", "fit": "does_not_qualify", "fit_explanation": "Human decision", "needs_recheck": False}
    monitor = {"id": account["id"], "account_id": account["id"], "remote_id": "synthetic-monitor", "local_token": "synthetic-token"}
    signal = {"company": account["name"], "domain": account["domain"], "type": "opening", "description": "Synthetic test opening", "location": "Portland, Oregon", "eventDate": "2026-10-01", "publicationDate": "2026-09-01", "eventDateStatus": "actual", "publicationDateStatus": "known"}
    citation = {"url": "https://synthetic.example/news", "title": "Synthetic test"}
    output = {"results": [citation], "content": {"signals": [signal]}, "grounding": [{"field": f"signals[0].{key}", "citations": [citation], "confidence": "high"} for key in signal]}
    event = {"id": "synthetic-event", "type": "monitor.run.completed", "data": {"id": "synthetic-run", "monitorId": monitor["remote_id"], "status": "completed", "output": output, "metadata": {"account_id": account["id"], "project": "account-watch"}}}
    store.put("accounts", account)
    store.put("monitors", monitor)
    return store, account, monitor, event


def signed(raw, timestamp, secret="synthetic-secret"):
    digest = hmac.new(secret.encode(), str(timestamp).encode() + b"." + raw, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def test_exact_hmac_and_retry_age():
    raw = b'{ "synthetic": true }'
    header = signed(raw, 1000)
    assert verify_webhook(raw, header, "synthetic-secret", now=1300)
    assert not verify_webhook(raw, header, "synthetic-secret", now=1301)
    assert not verify_webhook(b'{"synthetic":true}', header, "synthetic-secret", now=1000)
    assert not verify_webhook(raw, header, "wrong", now=1000)
    assert not verify_webhook(raw, header + ",t=1000", "synthetic-secret", now=1000)
    assert verify_webhook(raw, signed(raw, 1400), "synthetic-secret", now=1400)
    assert not verify_webhook(raw, signed(raw, 1400), "synthetic-secret", now=1000)


def test_atomic_idempotence_and_human_fit(setup):
    store, account, monitor, event = setup
    persist_delivery(store, monitor, event)
    assert apply_monitor_update(store, event["id"])["added"] == 1
    assert apply_monitor_update(store, event["id"])["status"] == "duplicate"
    updated = store.get("accounts", account["id"])
    assert updated["fit"] == account["fit"] and updated["fit_explanation"] == "Human decision"
    assert updated["needs_recheck"] is True
    assert store.list("signals")[0]["status"] == "supported"
    different_event = copy.deepcopy(event)
    different_event["id"] += "-retry"
    persist_delivery(store, monitor, different_event)
    assert apply_monitor_update(store, different_event["id"])["status"] == "duplicate"
    assert len(store.list("accounts")) == len(store.list("signals")) == 1


def test_concurrent_deliveries(setup):
    store, _, monitor, event = setup
    def deliver(index):
        payload = copy.deepcopy(event)
        payload["id"] += str(index)
        persist_delivery(store, monitor, payload)
        return apply_monitor_update(store, payload["id"])
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(deliver, range(8)))
    assert sum(result["added"] for result in results) == 1
    assert len(store.list("signals")) == 1


def test_transaction_failure_recovers(setup, monkeypatch):
    store, _, monitor, event = setup
    persist_delivery(store, monitor, event)
    original = store.put
    def fail(kind, record, **kwargs):
        if kind == "accounts":
            raise RuntimeError("Synthetic crash")
        return original(kind, record, **kwargs)
    monkeypatch.setattr(store, "put", fail)
    with pytest.raises(RuntimeError):
        apply_monitor_update(store, event["id"])
    assert store.list("signals") == []
    assert store.get("deliveries", event["id"])["status"] == "pending"
    monkeypatch.setattr(store, "put", original)
    assert apply_monitor_update(store, event["id"])["added"] == 1


@pytest.mark.parametrize("mutation", ["identity", "location", "date", "grounding", "old"])
def test_field_specific_validation(setup, mutation):
    _, account, _, event = setup
    output = event["data"]["output"]
    signal = output["content"]["signals"][0]
    if mutation == "identity":
        signal["domain"] = "unrelated.example"
    elif mutation == "location":
        signal["location"] = "Boston, Massachusetts"
    elif mutation == "date":
        signal["eventDate"] = None
    elif mutation == "old":
        signal["eventDate"] = "2000-01-01"
    else:
        output["grounding"] = [{"field": "content", "citations": output["results"]}]
    assert normalize_signals(account, output)[0]["status"] == "needs_review"


def test_multiple_signals_same_article(setup):
    _, account, _, event = setup
    output = event["data"]["output"]
    second = copy.deepcopy(output["content"]["signals"][0])
    second["location"] = "Seattle, Washington"
    output["content"]["signals"].append(second)
    assert len({s["id"] for s in normalize_signals(account, output)}) == 2


def test_empty_failed_and_wrong_identity(setup):
    store, account, monitor, event = setup
    bad = copy.deepcopy(event)
    bad["data"]["monitorId"] = "wrong"
    with pytest.raises(ValueError):
        persist_delivery(store, monitor, bad)
    bad["data"]["monitorId"] = monitor["remote_id"]
    bad["data"]["metadata"]["account_id"] = "wrong"
    with pytest.raises(ValueError):
        persist_delivery(store, monitor, bad)
    event["data"]["output"] = {"results": [], "content": {"signals": []}, "grounding": []}
    persist_delivery(store, monitor, event)
    assert apply_monitor_update(store, event["id"])["added"] == 0
    assert store.get("monitors", account["id"])["last_result"] == "No new findings"
    event["id"] = "synthetic-failed"
    event["data"].update(id="failed-run", status="failed", output=None, failReason="search_failed")
    persist_delivery(store, monitor, event)
    assert apply_monitor_update(store, event["id"])["status"] == "error"


def test_receiver_isolated_routes_and_raw_signature(setup, tmp_path):
    store, _, monitor, event = setup
    settings = Settings(secrets_path=tmp_path / "secrets")
    save_secret(settings.secrets_path, monitor["local_token"], "synthetic-secret")
    assert (settings.secrets_path / monitor["local_token"]).stat().st_mode & 0o777 == 0o600
    client = TestClient(create_app(settings, store))
    raw = json.dumps(event, indent=2).encode()
    path = "/webhooks/exa/" + monitor["local_token"]
    assert client.post(path, content=raw, headers={"Exa-Signature": signed(raw, int(time.time()))}).json()["added"] == 1
    assert client.post(path, content=raw, headers={"Exa-Signature": "invalid"}).status_code == 401
    assert client.get("/api/state").status_code == 404
    assert client.get("/docs").status_code == 404


def test_monitor_schema_pause_and_uncertain_creation(setup, tmp_path):
    store, account, _, _ = setup
    request = build_monitor_request(account, "https://receiver.example", "token")
    assert "trigger" not in request
    assert set(request["search"]) == {"query", "numResults", "contents"}
    assert GEOGRAPHY in request["search"]["query"] and "Texas" not in request["search"]["query"]
    assert len(request["outputSchema"]["properties"]["signals"]["items"]["properties"]) == 9
    class Client:
        calls = []
        async def request(self, method, path, body=None, **kwargs):
            self.calls.append((method, path, body))
            return {"status": "paused", "trigger": {"type": "interval", "period": "1d"}}
    client = Client()
    assert asyncio.run(set_monitor_schedule(store, client, account["id"], False))["status"] == "paused"
    assert client.calls[0][2] == {"status": "paused"}
    store.put("monitors", {"id": account["id"], "account_id": account["id"], "status": "creation_uncertain"})
    with pytest.raises(ValueError, match="uncertain"):
        asyncio.run(create_account_monitor(store, client, Settings(receiver_url="https://receiver.example", secrets_path=tmp_path / "secrets"), account["id"]))
    assert len(client.calls) == 1


def test_malformed_fields_and_new_monitor_window(setup):
    _, account, _, event = setup
    output = event["data"]["output"]
    signal = output["content"]["signals"][0]
    signal["eventDate"] = "2027-01-01"
    signal["publicationDate"] = "2027-01-01"
    assert normalize_signals(account, output, "2027-01-02T00:00:00Z")[0]["status"] == "supported"
    signal["description"] = ["invalid"]
    with pytest.raises(ValueError, match="description"):
        normalize_signals(account, output)


def test_receiver_resumes_durable_inbox(setup, tmp_path):
    store, _, monitor, event = setup
    persist_delivery(store, monitor, event)
    with TestClient(create_app(Settings(secrets_path=tmp_path / "secrets"), store)) as client:
        assert client.get("/health").status_code == 200
    assert store.get("deliveries", event["id"])["status"] == "applied"


def test_reconciliation_missing_secret_never_enables(setup, tmp_path):
    store, account, monitor, _ = setup
    class Client:
        async def request(self, method, path, body=None, **kwargs):
            if method == "GET":
                return {"data": [{"id": monitor["remote_id"], "metadata": {"account_id": account["id"], "project": "account-watch"}, "status": "active"}], "hasMore": False}
            assert body == {"status": "paused"}
            return {"status": "paused", "trigger": None}
    client = Client()
    result = asyncio.run(reconcile_monitor(store, client, Settings(secrets_path=tmp_path / "secrets"), account["id"]))
    assert result["status"] == "secret_unavailable"
    asyncio.run(set_monitor_schedule(store, client, account["id"], False))
    with pytest.raises(ValueError, match="secret unavailable"):
        asyncio.run(trigger_check(store, client, account["id"]))
    with pytest.raises(ValueError, match="secret unavailable"):
        asyncio.run(set_monitor_schedule(store, client, account["id"], True))


def test_receiver_reads_shared_tolerance(setup, tmp_path):
    store, _, monitor, event = setup
    settings = Settings(secrets_path=tmp_path / "secrets")
    save_secret(settings.secrets_path, monitor["local_token"], "synthetic-secret")
    client = TestClient(create_app(settings, store))
    raw = json.dumps(event).encode()
    headers = {"Exa-Signature": signed(raw, int(time.time()) - 500)}
    path = "/webhooks/exa/" + monitor["local_token"]
    assert client.post(path, content=raw, headers=headers).status_code == 401
    store.put("config", {"id": "public", "signature_tolerance": 600})
    assert client.post(path, content=raw, headers=headers).status_code == 200


@pytest.mark.parametrize("location", ["Sacramento, CA", "Sacramento, California", "Portland, OR", "Portland, Oregon", "Seattle, WA", "Seattle, Washington", "Seattle WA 98101, USA"])
def test_monitor_accepts_west_coast_states(setup, location):
    _, account, _, event = setup
    output = event["data"]["output"]
    output["content"]["signals"][0]["location"] = location
    assert normalize_signals(account, output, "2026-11-06T00:00:00Z")[0]["status"] == "supported"


@pytest.mark.parametrize("location", ["Austin, Texas", "Austin, TX", "Reno, NV", "Washington, DC", "Washington D.C.", "Washington Street, Boston, MA", "California Street, Reno, NV", "Portland or elsewhere", "CAFE", None])
def test_monitor_rejects_non_regional_or_ambiguous_locations(setup, location):
    _, account, _, event = setup
    output = event["data"]["output"]
    output["content"]["signals"][0]["location"] = location
    result = normalize_signals(account, output, "2026-11-06T00:00:00Z")[0]
    assert result["status"] == "needs_review"
    assert f"{GEOGRAPHY} location not established" in result["review_reasons"]


@pytest.mark.parametrize("explicit", [False, True])
def test_legacy_monitor_keeps_texas_scope(setup, explicit):
    _, account, _, event = setup
    account.pop("geography")
    if explicit:
        account["geography"] = "Texas"
    output = event["data"]["output"]
    assert normalize_signals(account, output, "2026-11-06T00:00:00Z")[0]["status"] == "needs_review"
    output["content"]["signals"][0]["location"] = "Austin, Texas"
    assert normalize_signals(account, output, "2026-11-06T00:00:00Z")[0]["status"] == "supported"
    assert "'geography': 'Texas'" in build_monitor_request(account, "https://receiver.example", "token")["search"]["query"]


def test_custom_monitor_window_and_review(setup):
    _, account, _, event = setup
    account.update(geography="Canada", accounts="Dentists", qualifications="Hiring", window_days=14)
    signal = normalize_signals(account, event["data"]["output"], "2026-11-06T00:00:00Z")[0]
    assert signal["status"] == "needs_review"
    assert "Event outside monitoring 14-day window" in signal["review_reasons"]
    assert any("Custom geography" in reason for reason in signal["review_reasons"])
