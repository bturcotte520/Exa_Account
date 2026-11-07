"""Synthetic tests only; none of these records represent customer evidence."""
import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent import apply_research_result, build_account_task, normalize_research, poll_account_research, queue_research, start_account_research
from app.api import ExaClient, ExaError, sanitize
from app.config import Settings, load_settings
from app.contents import read_more_context
from app.main import create_app
from app.models import Criteria, GEOGRAPHY, criteria_snapshot, normalize_domain, normalize_url
from app.search import build_discovery_queries, build_location_query, confirm_candidate, create_session, discover_companies, search_locations, select_candidate
from app.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "test.sqlite3")


@pytest.fixture
def account(store):
    value = {"id": "account_synthetic", "name": "Synthetic Fitness", "domain": "synthetic.example.com", "domain_verified": True,
             "session_id": "session_synthetic", "window_start": "2026-08-08", "window_end": "2026-11-06",
             "geography": GEOGRAPHY, "locations": [], "expansion_signals": [], "fit": "needs_review", "fit_explanation": "Not researched"}
    return store.put("accounts", value)


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def request(self, method, path, body=None, **kwargs):
        self.calls.append((method, path, body, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)


def grounded_response(account):
    data = {"company_name": account["name"], "domain": account["domain"], "entity_verification_status": "verified",
            "locations": [{"name": "Synthetic A", "address": "1 Demo Street, Sacramento", "state": "CA", "operating_status": "operating", "operating_entity": account["name"]},
                          {"name": "Synthetic B", "address": "2 Demo Street, Portland", "state": "OR", "operating_status": "operating", "operating_entity": account["name"]}],
            "expansion_signals": [{"type": "opening", "description": "Synthetic opening", "state": "WA", "event_date": "2026-10-01", "publication_date": "2026-10-02", "date_status": "actual"}],
            "employee_count": None, "fit": "qualifies", "fit_explanation": "Synthetic criteria supported"}
    fields = ["domain", "entity_verification_status", "fit", "fit_explanation"]
    fields += [f"locations[{index}].{field}" for index in range(2) for field in ("address", "state", "operating_status", "operating_entity")]
    fields += [f"expansion_signals[0].{field}" for field in ("type", "description", "state", "event_date", "date_status")]
    return {"id": "agent_run_synthetic", "status": "completed", "output": {"structured": data, "text": "Synthetic test result",
            "grounding": [{"field": field, "citations": [{"url": "https://synthetic.example.com/source"}]} for field in fields]}}


def test_store_atomic_rollback_and_optional_connection(store):
    with pytest.raises(RuntimeError):
        with store.transaction() as conn:
            store.put("accounts", {"id": "a"}, conn=conn)
            assert store.get("accounts", "a", conn=conn)
            assert len(store.list("accounts", conn=conn)) == 1
            raise RuntimeError("rollback")
    assert store.get("accounts", "a") is None
    with pytest.raises(ValueError):
        store.put("accounts", {})


def test_store_memory_shared():
    memory = Store(":memory:")
    memory.put("accounts", {"id": "a"})
    assert memory.get("accounts", "a") == {"id": "a"}


def test_domain_and_url_normalization():
    assert normalize_domain("https://WWW.Fitness.com/about") == "fitness.com"
    assert normalize_url("https://www.fitness.com/a/?utm_source=test#x") == "https://fitness.com/a"
    for bad in ("localhost", "http://127.0.0.1", "https://user:pass@fitness.com", "https://gym.local", "ftp://fitness.com"):
        with pytest.raises(ValueError):
            normalize_domain(bad)


def test_discovery_query_limits():
    bodies = build_discovery_queries("2026-08-08", "2026-11-06")
    assert len(bodies) == 2
    assert all(b["type"] == "auto" and b["numResults"] == 10 and b["contents"] == {"highlights": True} for b in bodies)
    assert "startPublishedDate" not in bodies[0]
    assert bodies[1]["startPublishedDate"].startswith("2026-08-08")
    assert all(GEOGRAPHY in body["query"] and "Texas" not in body["query"] for body in bodies)
    assert "Nevada" in build_discovery_queries("2026-08-08", "2026-11-06", "Nevada")[0]["query"]


def test_discovery_dedup_and_bounded_candidates(store):
    session = create_session(store)
    assert session["geography"] == GEOGRAPHY
    results = [{"url": f"https://gym{i}.example.com", "title": f"Synthetic {i}", "highlights": ["Synthetic"]} for i in range(12)]
    client = FakeClient({"requestId": "q1", "results": results}, {"requestId": "q2", "results": [{**results[0], "url": results[0]["url"] + "/?utm_source=x"}]})
    asyncio.run(discover_companies(store, client, session["id"]))
    assert len(client.calls) == 2
    assert len(store.list("candidates")) == 10
    assert len(store.list("evidence")) == 12
    assert len(store.list("evidence")[0]["appearances"]) == 2
    assert not any(c["domain_verified"] for c in store.list("candidates"))
    with pytest.raises(ValueError):
        asyncio.run(discover_companies(store, client, session["id"]))


def seed_candidates(store, count=6):
    session = create_session(store)
    for index in range(count):
        store.put("candidates", {"id": str(index), "session_id": session["id"], "name": f"Synthetic {index}",
                                 "domain": f"gym{index}.example.com", "domain_verified": False, "selected": False, "evidence_ids": []})
    return session


def test_confirmation_selection_cap_and_domain_upsert(store):
    seed_candidates(store)
    with pytest.raises(ValueError):
        select_candidate(store, "0")
    with pytest.raises(ValueError):
        confirm_candidate(store, "0", "linkedin.com")
    for index in range(6):
        confirm_candidate(store, str(index), f"gym{index}.example.com")
    with ThreadPoolExecutor(max_workers=6) as pool:
        def attempt(index):
            try:
                return select_candidate(store, str(index))
            except ValueError:
                return None
        results = list(pool.map(attempt, range(6)))
    assert sum(r is not None for r in results) == 5
    assert len(store.list("accounts")) == 5
    selected = next(c for c in store.list("candidates") if c["selected"])
    select_candidate(store, selected["id"])
    assert len(store.list("accounts")) == 5


def test_two_candidates_same_domain_share_account(store):
    seed_candidates(store, 2)
    for index in range(2):
        confirm_candidate(store, str(index), "https://www.same.example.com")
        select_candidate(store, str(index))
    assert len(store.list("accounts")) == 1


def test_grounded_qualification_and_uncertain_fiber(account):
    result = normalize_research(account, grounded_response(account))
    assert result["fit"] == "qualifies"
    assert result["locations_found"] == 2
    assert result["employee_count"] is None
    assert result["provenance"]["employee_count"]["provider"] == "unconfirmed"
    task = build_account_task(account, [])
    assert task["dataSources"] == [{"provider": "fiber"}]
    assert task["effort"] == "medium"
    assert GEOGRAPHY in task["query"] and "Texas" not in task["query"]
    assert "dataSources" not in build_account_task(account, [], web_only=True)


@pytest.mark.parametrize("change", ["announced", "duplicate", "missing_grounding", "root_grounding", "unknown_date", "old_date", "future_date", "planned", "wrong_entity", "wrong_region", "low_confidence"])
def test_required_criteria_cannot_be_inferred(account, change):
    response = grounded_response(account)
    data = response["output"]["structured"]
    if change == "announced":
        data["locations"][1]["operating_status"] = "announced"
    elif change == "duplicate":
        data["locations"][1]["address"] = "1 Demo Street, Sacramento."
    elif change == "missing_grounding":
        response["output"]["grounding"] = response["output"]["grounding"][:-1]
    elif change == "root_grounding":
        response["output"]["grounding"] = [{"field": "locations", "citations": [{"url": "https://synthetic.example.com"}]}]
    elif change in {"unknown_date", "old_date", "future_date"}:
        data["expansion_signals"][0]["event_date"] = {"unknown_date": None, "old_date": "2025-01-01", "future_date": "2027-01-01"}[change]
    elif change == "planned":
        data["expansion_signals"][0]["date_status"] = "planned"
    elif change == "wrong_entity":
        data["locations"][1]["operating_entity"] = "Unrelated Franchisee"
    elif change == "wrong_region":
        data["expansion_signals"][0]["state"] = "TX"
    elif change == "low_confidence":
        response["output"]["grounding"][0]["confidence"] = "low"
    assert normalize_research(account, response)["fit"] == "needs_review"


def test_output_domain_mismatch_null_and_invalid_shape(account):
    response = grounded_response(account)
    response["output"]["structured"]["domain"] = "different.example.com"
    with pytest.raises(ValueError):
        normalize_research(account, response)
    response["output"]["structured"] = None
    with pytest.raises(ValueError):
        normalize_research(account, response)


def test_review_preserved_and_grounding_evidence_persisted(store, account):
    account.update(fit="does_not_qualify", fit_explanation="Human decision")
    store.put("accounts", account)
    store.put("reviews", {"id": "review", "account_id": account["id"]})
    run = {"id": "research", "account_id": account["id"]}
    apply_research_result(store, run, grounded_response(account))
    changed = store.get("accounts", account["id"])
    assert changed["fit"] == "does_not_qualify" and changed["needs_recheck"]
    assert store.list("evidence")


def test_location_lookup_reused_even_if_empty(store, account):
    client = FakeClient({"results": []})
    assert asyncio.run(search_locations(store, client, account)) == []
    assert asyncio.run(search_locations(store, client, account)) == []
    assert len(client.calls) == 1
    assert GEOGRAPHY in client.calls[0][2]["query"]


def test_places_search_exact_supported_payload_and_inline_retention(store, account):
    body = build_location_query(account)
    assert body == {"query": f"Physical locations of Synthetic Fitness (synthetic.example.com) in {GEOGRAPHY}: street addresses, local business listings, opening hours and operating company. Distinguish operating branches from planned or announced locations and unrelated franchisees.",
                    "type": "auto", "numResults": 5, "contents": {"text": True, "highlights": True}}
    result = {"url": "https://exa.ai/library/places/synthetic", "title": "Synthetic places listing",
              "text": "Synthetic local listing: address, hours and website", "highlights": ["Local listing"],
              "extra": {"returned": "provider metadata"}}
    client = FakeClient({"requestId": "places-query", "results": [result]})
    evidence = asyncio.run(search_locations(store, client, account))
    assert client.calls[0][:3] == ("POST", "/search", body)
    assert evidence[0]["text"] == result["text"]
    assert evidence[0]["raw"] == result
    assert "Places structure only if present" in evidence[0]["metadata_basis"]
    task = build_account_task(account, evidence)
    assert result["text"] in task["query"]


def test_preview_routes_are_local_and_use_real_builders(store, account, tmp_path):
    client = FakeClient()
    settings = Settings(db_path=tmp_path / "unused.sqlite3", receiver_url="https://receiver.example.com")
    with TestClient(create_app(settings, store, client)) as ui:
        criteria = {"geography": "Japan", "accounts": "Hotels", "qualifications": "Renovated", "window_start": "2026-01-01", "window_end": "2026-02-01"}
        preview = ui.post("/api/discovery/preview", json=criteria)
        assert preview.status_code == 200
        assert preview.json()["requests"] == build_discovery_queries("2026-01-01", "2026-02-01", "Japan", "Hotels", "Renovated")
        assert ui.post("/api/discovery/preview", json={**criteria, "window_end": "2025-01-01"}).status_code == 422
        value = ui.get(f"/api/accounts/{account['id']}/monitor/preview").json()
        from app.monitors import build_monitor_request
        assert value["request"] == build_monitor_request(account, settings.receiver_url, "assigned-on-create")
        assert value["receiver_configured"] is True
        assert ui.get(f"/api/accounts/{account['id']}").json()["location_request"] == build_location_query(account)
    assert client.calls == []
    assert store.list("monitors") == []


def test_research_api_starts_worker_and_reports_failure(store, account, tmp_path):
    import threading
    called = threading.Event()

    class FailingClient:
        async def request(self, method, path, body=None, **kwargs):
            assert path == "/search"
            called.set()
            raise ExaError("Synthetic API unavailable")

    with TestClient(create_app(Settings(db_path=tmp_path / "unused.sqlite3"), store, FailingClient())) as ui:
        assert ui.post("/api/research", json={"account_ids": []}).status_code == 422
        response = ui.post("/api/research", json={"account_ids": [account["id"]]})
        assert response.status_code == 202
        assert called.wait(2), "Research worker never started"
        # Reads run on the same application loop after the worker's synchronous failure save.
        run = ui.get("/api/state").json()["agent_runs"][0]
        assert run["status"] == "failed" and run["stage"] == "locations" and run["error"]
        assert "Synthetic API unavailable" in run["error"]
        rejected = ui.post("/api/research", json={"account_ids": [account["id"]]})
        assert rejected.status_code == 400 and "already has research" in rejected.json()["detail"]


def test_occupied_queue_explains_wait_without_paid_request(store, account, tmp_path):
    client = FakeClient()
    with TestClient(create_app(Settings(db_path=tmp_path / "unused.sqlite3"), store, client)) as ui:
        for i in range(2):
            store.put("agent_runs", {"id": f"unresolved{i}", "account_id": f"other{i}", "status": "ambiguous"})
        assert ui.post("/api/research", json={"account_ids": [account["id"]]}).status_code == 202
        runs = ui.get("/api/state").json()["agent_runs"]
        queued = next(run for run in runs if run["account_id"] == account["id"])
        assert queued["status"] == "queued_local"
        assert "Waiting for capacity" in queued["queue_note"]
    assert client.calls == []


def test_research_worker_completes_source_backed_account_without_network(store, account, tmp_path):
    client = FakeClient({"results": [{"url": "https://synthetic.example.com/locations", "text": "Synthetic operating sites"}]}, grounded_response(account))
    with TestClient(create_app(Settings(db_path=tmp_path / "unused.sqlite3"), store, client)) as ui:
        assert ui.post("/api/research", json={"account_ids": [account["id"]]}).status_code == 202
        detail = ui.get(f"/api/accounts/{account['id']}").json()
        assert detail["agent_runs"][0]["status"] == "completed"
        assert detail["account"]["fit"] == "qualifies"
        assert detail["account"]["locations_found"] == 2
        assert any(e.get("text") == "Synthetic operating sites" for e in detail["evidence"])
        assert len(client.calls) == 2
        assert client.calls[1][1] == "/agent/runs"
        assert client.calls[1][2]["dataSources"] == [{"provider": "fiber"}]
        assert store.list("monitors") == []


def test_create_timeout_not_retried_and_two_active_limit(store, account):
    async def execute():
        accounts = []
        for index in range(3):
            accounts.append(store.put("accounts", {**account, "id": f"account_{index}", "domain": f"gym{index}.example.com"}))
        runs = queue_research(store, [a["id"] for a in accounts])
        client = FakeClient({"results": []}, ExaError("timeout", ambiguous=True), {"results": []}, {"id": "remote_two", "status": "queued"})
        first = await start_account_research(store, client, runs[0]["id"])
        assert first["status"] == "ambiguous"
        await start_account_research(store, client, runs[1]["id"])
        third = await start_account_research(store, client, runs[2]["id"])
        assert third["status"] == "queued_local"
        assert len(client.calls) == 4
        with pytest.raises(ValueError):
            await start_account_research(store, client, runs[0]["id"])
    asyncio.run(execute())


def test_poll_failed_and_resume_existing_only(store, account):
    run = store.put("agent_runs", {"id": "run", "account_id": account["id"], "remote_id": "remote", "status": "resumable"})
    client = FakeClient({"id": "remote", "status": "failed", "error": {"message": "Fiber unavailable"}}, {"data": []})
    result = asyncio.run(poll_account_research(store, client, run["id"]))
    assert result["status"] == "failed"
    assert all(call[0] == "GET" for call in client.calls)
    assert store.get("accounts", account["id"])["fit"] == "needs_review"


def test_contents_failure_and_cap_preserve_passage(store, account):
    evidence = store.put("evidence", {"id": "e", "account_id": account["id"], "url": "https://synthetic.example.com/source", "highlights": ["Original passage"]})
    client = FakeClient({"statuses": [{"id": evidence["url"], "status": "error"}], "results": []}, ExaError("timeout"))
    for _ in range(2):
        context = asyncio.run(read_more_context(store, client, account["id"], "e"))
        assert context["status"] == "failed"
        assert context["previous_passage"] == ["Original passage"]
    with pytest.raises(ValueError):
        asyncio.run(read_more_context(store, client, account["id"], "e"))
    assert store.get("evidence", "e")["highlights"] == ["Original passage"]
    assert store.get("accounts", account["id"])["fit"] == "needs_review"


def test_contents_foreign_source_rejected(store, account):
    store.put("evidence", {"id": "other", "account_id": "another", "url": "https://synthetic.example.com"})
    client = FakeClient()
    with pytest.raises(ValueError):
        asyncio.run(read_more_context(store, client, account["id"], "other"))
    assert not client.calls


def test_redaction():
    value = {"webhookSecret": "private", "local_token": "opaque", "jev_key": "unused", "fin_data": "unused2", "nested": ["secret-in-text"], "url": "https://receiver.example.com/webhooks/exa/opaque"}
    dumped = json.dumps(sanitize(value, ["secret-in-text"]))
    assert not any(secret in dumped for secret in ("private", "opaque", "unused", "secret-in-text"))


def test_api_artifacts_and_safe_retry(monkeypatch, store):
    real_async_client = httpx.AsyncClient
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, json={"error": "temporary"})
        return httpx.Response(200, json={"id": "remote", "webhookSecret": "secret-response", "costDollars": {"total": 0.1}})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_async_client(transport=httpx.MockTransport(handler), **kwargs))
    client = ExaClient(Settings(api_key="secret-api"), store)
    result = asyncio.run(client.request("GET", "/agent/runs/remote"))
    assert result["webhookSecret"] == "secret-response"
    artifacts = store.list("artifacts")
    assert len(artifacts) == 2
    assert "secret-response" not in json.dumps(artifacts)
    assert "secret-api" not in json.dumps(artifacts)
    assert artifacts[-1]["cost_dollars"] == {"total": 0.1}


@pytest.mark.parametrize("method,status,expected", [("POST", 503, 1), ("GET", 401, 1), ("GET", 402, 1), ("GET", 503, 3)])
def test_request_retry_boundaries(monkeypatch, store, method, status, expected):
    real_async_client = httpx.AsyncClient
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "test"})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_async_client(transport=httpx.MockTransport(handler), **kwargs))
    with pytest.raises(ExaError):
        asyncio.run(ExaClient(Settings(api_key="mock"), store).request(method, "/agent/runs"))
    assert len(calls) == expected


def test_config_exact_key_without_reading_real_env(monkeypatch):
    monkeypatch.delenv("exa_key", raising=False)
    monkeypatch.setenv("EXA_API_KEY", "wrong-case")
    assert load_settings().api_key == ""
    monkeypatch.setenv("exa_key", "synthetic-key")
    assert load_settings().api_key == "synthetic-key"
    assert "synthetic-key" not in repr(load_settings())


def test_private_app_startup_state_and_csrf(store, account):
    store.put("agent_runs", {"id": "run", "account_id": account["id"], "status": "running", "remote_id": "remote"})
    fake = FakeClient()
    app = create_app(Settings(), store, fake)
    with TestClient(app) as client:
        state = client.get("/api/state").json()
        assert state["agent_runs"][0]["status"] == "resumable"
        assert not fake.calls
        assert client.get("/api/accounts/" + account["id"]).json()["account"]["id"] == account["id"]
        assert client.post("/api/discovery", json={}, headers={"Origin": "https://evil.example.com"}).status_code == 403
        assert client.post("/api/discovery", content="{}", headers={"Content-Type": "text/plain"}).status_code == 415
        assert client.get("/api/state", headers={"Host": "evil.example.com"}).status_code == 400
        assert "api_key" not in client.get("/api/config").json()
        assert client.get("/api/config").json()["geography"] == GEOGRAPHY
        assert client.post("/api/discovery", json={"geography": " "}).status_code == 422


def test_merge_preserves_evidence_and_history(store, account):
    target = store.put("accounts", {**account, "id": "target", "domain": "target.example.com"})
    store.put("evidence", {"id": "e", "account_id": account["id"], "url": "https://synthetic.example.com"})
    with TestClient(create_app(Settings(), store, FakeClient())) as client:
        result = client.post(f"/api/accounts/{account['id']}/merge", json={"target_id": target["id"]})
    assert result.status_code == 200
    assert store.get("evidence", "e")["account_id"] == "target"
    assert store.get("accounts", "target")["merge_history"][0]["source"]["id"] == account["id"]
    assert store.get("accounts", account["id"])["merged_into"] == "target"


def test_selection_follows_merged_domain_alias(store, account):
    store.put("accounts", {**account, "id": "target", "domain": "target.example.com"})
    store.put("accounts", {**account, "merged_into": "target"})
    session = seed_candidates(store, 1)
    candidate = store.get("candidates", "0")
    candidate.update(selected=True, account_id=account["id"], domain=account["domain"], domain_verified=True)
    store.put("candidates", candidate)
    account["session_id"] = session["id"]
    store.put("accounts", {**account, "merged_into": "target"})
    result = select_candidate(store, "0")
    assert result["account"]["id"] == "target"
    assert store.get("accounts", account["id"])["merged_into"] == "target"


def test_unexpected_remote_status_keeps_id_and_concurrency_reservation(store, account):
    run = queue_research(store, [account["id"]])[0]
    client = FakeClient({"results": []}, {"id": "remote", "status": "new_unknown_status"})
    result = asyncio.run(start_account_research(store, client, run["id"]))
    assert result["remote_id"] == "remote"
    assert result["status"] == "ambiguous"


@pytest.mark.parametrize("state", ["CA", "California", "OR", "Oregon", "WA", "Washington", " ca ", "oregon"])
def test_west_coast_states_qualify(account, state):
    response = grounded_response(account)
    for location in response["output"]["structured"]["locations"]:
        location["state"] = state
    response["output"]["structured"]["expansion_signals"][0]["state"] = state
    assert normalize_research(account, response)["fit"] == "qualifies"


@pytest.mark.parametrize("state", ["TX", "Texas", "NV", "AZ", "AK", "HI", "DC", "Washington, DC", None])
@pytest.mark.parametrize("field", ["locations", "expansion_signals"])
def test_outside_region_does_not_qualify(account, state, field):
    response = grounded_response(account)
    response["output"]["structured"][field][0]["state"] = state
    result = normalize_research(account, response)
    assert result["fit"] == "needs_review"
    if field == "locations":
        assert result["locations_found"] == 1


@pytest.mark.parametrize("explicit", [False, True])
def test_legacy_account_keeps_texas_research(store, account, explicit):
    account.pop("geography")
    if explicit:
        account["geography"] = "Texas"
    store.put("accounts", account)
    response = grounded_response(account)
    assert normalize_research(account, response)["fit"] == "needs_review"
    for item in response["output"]["structured"]["locations"] + response["output"]["structured"]["expansion_signals"]:
        item["state"] = "TX"
    assert normalize_research(account, response)["fit"] == "qualifies"
    assert "Geography: Texas" in build_account_task(account, [])["query"]
    client = FakeClient({"results": []})
    asyncio.run(search_locations(store, client, account))
    assert "in Texas" in client.calls[0][2]["query"]


@pytest.mark.parametrize("explicit", [False, True])
def test_same_domain_new_region_does_not_reuse_history(store, account, explicit):
    legacy = {**account, "geography": "Texas", "fit": "qualifies", "location_search_status": "completed"}
    if not explicit:
        legacy.pop("geography")
    store.put("accounts", legacy)
    session = seed_candidates(store, 1)
    confirm_candidate(store, "0", account["domain"])
    fresh = select_candidate(store, "0")["account"]
    assert fresh["id"] != legacy["id"]
    assert fresh["geography"] == GEOGRAPHY and fresh["session_id"] == session["id"]
    assert fresh["fit"] == "needs_review" and fresh["locations"] == []
    assert "location_search_status" not in fresh
    assert store.get("accounts", legacy["id"]) == legacy
    assert select_candidate(store, "0")["account"]["id"] == fresh["id"]
    with TestClient(create_app(Settings(), store, FakeClient())) as client:
        result = client.post(f"/api/accounts/{legacy['id']}/merge", json={"target_id": fresh["id"]})
        assert result.status_code == 400
        assert "different research criteria" in result.json()["detail"]
    assert store.get("accounts", legacy["id"]) == legacy


def test_legacy_session_discovery_and_selection_keep_scope(store):
    session = seed_candidates(store, 1)
    session.pop("geography")
    store.put("sessions", session)
    client = FakeClient({"results": []}, {"results": []})
    asyncio.run(discover_companies(store, client, session["id"]))
    assert all("Texas" in call[2]["query"] for call in client.calls)
    confirm_candidate(store, "0", "legacy.example.com")
    assert select_candidate(store, "0")["account"]["geography"] == "Texas"


@pytest.mark.parametrize("values", [
    {"geography": " "}, {"accounts": ""}, {"qualifications": " "},
    {"window_days": 0}, {"window_days": True}, {"window_days": 2.5}, {"window_days": 3651},
    {"window_start": "2026-01-01"}, {"window_start": "2026-02-30", "window_end": "2026-03-01"},
    {"window_start": "2026-02-02", "window_end": "2026-02-01"},
    {"window_start": "2099-01-01", "window_end": "2099-02-01"},
])
def test_custom_criteria_validation(values):
    with pytest.raises(ValueError):
        Criteria(**values)


def test_custom_session_propagation_and_source_acceptance(store):
    from app.monitors import build_monitor_request
    from app.replay import export_csv, export_recording
    criteria = Criteria(geography="Ontario, Canada", accounts="Independent dental practices", qualifications="Accepting new patients", window_days=30)
    session = create_session(store, criteria)
    client = FakeClient(*[{"results": [{"url": "https://practice.example.com", "title": "Synthetic practice"}]}] * 2)
    asyncio.run(discover_companies(store, client, session["id"]))
    for call in client.calls:
        assert all(value in call[2]["query"] for value in (criteria.geography, criteria.accounts, criteria.qualifications))
    candidate = store.list("candidates")[0]
    result = select_candidate(store, candidate["id"])
    account = result["account"]
    assert account["domain_accepted"] and not account["domain_verified"]
    assert criteria_snapshot(account) == criteria_snapshot(session)
    run = queue_research(store, [account["id"]])[0]
    assert run["criteria"] == criteria_snapshot(session)
    task = build_account_task(account, [])
    assert all(value in task["query"] for value in (criteria.geography, criteria.accounts, criteria.qualifications))
    assert "operating fitness" not in task["query"]
    monitor = build_monitor_request(account, "https://receiver.example.com", "synthetic-token")
    assert "rolling 30-day" in monitor["search"]["query"]
    assert criteria.qualifications in monitor["search"]["query"]
    assert criteria.qualifications in export_csv(store)
    assert export_recording(store)["records"]["accounts"][0]["accounts"] == criteria.accounts
    assert Store(store.path).get("sessions", session["id"])["qualifications"] == criteria.qualifications
    # Same company in a later session must not inherit the old fit or window.
    fresh = create_session(store, Criteria(geography=criteria.geography, window_days=7))
    store.put("candidates", {**candidate, "id": "new", "session_id": fresh["id"], "domain_verified": True})
    second = select_candidate(store, "new")["account"]
    assert second["id"] != account["id"] and second["window_days"] == 7
    assert store.get("accounts", account["id"])["window_days"] == 30


def test_exact_dates_persist_and_compute_monitor_duration(store):
    session = create_session(store, Criteria(window_start="2026-01-01", window_end="2026-02-01"))
    assert (session["window_start"], session["window_end"], session["window_days"], session["window_mode"]) == ("2026-01-01", "2026-02-01", 31, "dates")


@pytest.mark.parametrize("domain", ["linkedin.com", "sub.yelp.com", "127.0.0.1", "bad..example.com", "-bad.example.com"])
def test_add_rejects_invalid_or_directory_domains(store, domain):
    session = seed_candidates(store, 1)
    store.put("evidence", {"id": "e", "session_id": session["id"], "url": "https://" + domain})
    candidate = store.get("candidates", "0")
    candidate.update(domain=domain, evidence_ids=["e"])
    store.put("candidates", candidate)
    with pytest.raises(ValueError):
        select_candidate(store, "0")
    assert store.list("accounts") == []


def test_generic_qualification_requires_each_grounded_criterion(account):
    account.update(geography="Japan", accounts="Software companies", qualifications="Profitable", window_days=30)
    response = grounded_response(account)
    response["output"]["structured"].update(locations=[], expansion_signals=[])
    assert normalize_research(account, response)["fit"] == "needs_review"
    assessments = {key: {"status": "met", "explanation": "Synthetic supporting evidence"} for key in ("geography", "accounts", "qualifications", "research_window")}
    response["output"]["structured"]["criteria_assessment"] = assessments
    for key in assessments:
        for field in ("status", "explanation"):
            response["output"]["grounding"].append({"field": f"criteria_assessment.{key}.{field}", "citations": [{"url": "https://synthetic.example.com/source"}]})
    result = normalize_research(account, response)
    assert result["fit"] == "qualifies" and result["assessment_basis"] == "source-grounded Agent assessment"
    response["output"]["grounding"].pop()
    assert normalize_research(account, response)["fit"] == "needs_review"


def test_discovery_endpoint_accepts_custom_snapshot(store):
    fake = FakeClient({"results": []}, {"results": []})
    with TestClient(create_app(Settings(), store, fake)) as client:
        response = client.post("/api/discovery", json={"geography": "France", "accounts": "Hotels", "qualifications": "Independent ownership", "window_days": 14})
        assert response.status_code == 202
        assert response.json()["accounts"] == "Hotels"
    assert store.list("sessions")[0]["window_days"] == 14


def test_new_default_accounts_require_account_type_evidence(account):
    account["accounts"] = "Regional fitness chains"
    response = grounded_response(account)
    assert normalize_research(account, response)["fit"] == "needs_review"
    response["output"]["structured"]["criteria_assessment"] = {"accounts": {"status": "met", "explanation": "Synthetic fitness chain evidence"}}
    for field in ("status", "explanation"):
        response["output"]["grounding"].append({"field": f"criteria_assessment.accounts.{field}", "citations": [{"url": "https://synthetic.example.com/about"}]})
    assert normalize_research(account, response)["fit"] == "qualifies"


def test_source_backed_agent_name_replaces_search_page_title(account):
    response = grounded_response(account)
    account["name"] = "Welcome to the official website"
    assert normalize_research(account, response)["fit"] == "needs_review"
    response["output"]["grounding"].append({"field": "company_name", "citations": [{"url": "https://synthetic.example.com/about"}]})
    result = normalize_research(account, response)
    assert result["fit"] == "qualifies" and result["name"] == "Synthetic Fitness"
