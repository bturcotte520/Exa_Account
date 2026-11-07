import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .agent import ACTIVE, poll_account_research, queue_research, start_account_research
from .api import ExaClient, ExaError, sanitize
from .config import load_settings
from .contents import read_more_context
from .models import GEOGRAPHY, Criteria, criteria_snapshot, new_id, now
from .search import build_discovery_queries, build_location_query, confirm_candidate, create_session, discover_companies, select_candidate
from .store import Store


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiscoveryInput(Criteria):
    pass


class Confirmation(Input):
    domain: str = Field(min_length=3, max_length=253)
    name: str | None = Field(default=None, max_length=250)
    note: str = Field(default="", max_length=4000)


class ResearchInput(Input):
    account_ids: list[str] = Field(min_length=1, max_length=5)
    web_only: bool = False


class ContextInput(Input):
    evidence_id: str
    query: str = Field(default="Clarify whether the location is operating or planned", min_length=1, max_length=2000)


class ReviewInput(Input):
    decision: Literal["confirm", "unresolved", "confirmed", "needs_review"]
    note: str = Field(default="", max_length=4000)
    evidence_id: str | None = None


class MergeInput(Input):
    target_id: str


class ConfigInput(Input):
    receiver_url: str | None = None
    signature_tolerance: int | None = Field(default=None, ge=1, le=86400)


def create_app(settings=None, store=None, client=None):
    settings = settings or load_settings()
    store = store or Store(settings.db_path)
    client = client or ExaClient(settings, store)
    tasks = set()
    worker_lock = asyncio.Lock()

    def spawn(coro):
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def research_worker():
        async with worker_lock:
            for _ in range(225):
                queued = [run for run in store.list("agent_runs") if run["status"] == "queued_local"]
                for run in queued:
                    await start_account_research(store, client, run["id"])
                polling = [run for run in store.list("agent_runs") if run["status"] in {"queued", "running"} and run.get("remote_id")]
                if not polling:
                    for run in store.list("agent_runs"):
                        if run["status"] == "queued_local":
                            run["queue_note"] = "Waiting for capacity: refresh or reconcile existing unresolved research runs. No additional paid request has been sent."
                            store.put("agent_runs", run)
                    return
                await asyncio.sleep(4)
                await asyncio.gather(*(poll_account_research(store, client, run["id"]) for run in polling))
            for run in store.list("agent_runs"):
                if run["status"] in {"queued", "running"}:
                    run.update(status="stalled", error="Automatic polling paused after 15 minutes; refresh the saved run")
                    store.put("agent_runs", run)

    @asynccontextmanager
    async def lifespan(app):
        # Restart never performs network calls or restarts a chargeable create.
        for run in store.list("agent_runs"):
            if run["status"] in {"starting", "queued", "running", "queued_local"}:
                run["status"] = "resumable" if run.get("remote_id") else "ambiguous" if run.get("stage") == "create" else "interrupted"
                store.put("agent_runs", run)
        for session in store.list("sessions"):
            if session["status"] in {"queued", "running"}:
                session["status"] = "interrupted"
                store.put("sessions", session)
        yield
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="Account Watch", lifespan=lifespan)
    app.state.store, app.state.client, app.state.settings = store, client, settings
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "Cross-origin writes are not allowed"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Cross-site writes are not allowed"}, status_code=403)
            if request.headers.get("content-length", "0") != "0" and request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"detail": "Use application/json"}, status_code=415)
        return await call_next(request)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(ExaError)
    async def upstream(request, exc):
        return JSONResponse({"detail": str(exc), "ambiguous": exc.ambiguous}, status_code=502)

    def required(kind, id):
        record = store.get(kind, id)
        if record is None:
            raise HTTPException(404, f"{kind} record not found")
        return record

    def public(value):
        return sanitize(value, (settings.api_key,))

    @app.get("/api/state")
    def state():
        return public({kind: store.list(kind) for kind in ("sessions", "candidates", "accounts", "artifacts", "monitors", "deliveries", "evidence", "agent_runs", "reviews", "signals", "recordings")})

    @app.get("/api/config")
    def config():
        return {"api_key_configured": bool(settings.api_key), "receiver_url": settings.receiver_url,
                "signature_tolerance": settings.signature_tolerance, "geography": GEOGRAPHY, "window_days": 90}

    @app.patch("/api/config")
    def configure(body: ConfigInput):
        if body.receiver_url is not None:
            parsed = urlsplit(body.receiver_url)
            if body.receiver_url and (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise ValueError("Receiver URL must be HTTPS without credentials, query, or fragment")
            settings.receiver_url = body.receiver_url.rstrip("/")
        if body.signature_tolerance is not None:
            settings.signature_tolerance = body.signature_tolerance
        store.put("config", {"id": "public", "receiver_url": settings.receiver_url, "signature_tolerance": settings.signature_tolerance})
        return config()

    saved_config = store.get("config", "public")
    if saved_config:
        settings.receiver_url = saved_config.get("receiver_url", settings.receiver_url)
        settings.signature_tolerance = saved_config.get("signature_tolerance", settings.signature_tolerance)

    @app.post("/api/discovery", status_code=202)
    async def discover(body: DiscoveryInput = DiscoveryInput()):
        session = create_session(store, body)
        spawn(discover_companies(store, client, session["id"]))
        return session

    @app.post("/api/discovery/preview")
    def discovery_preview(body: DiscoveryInput):
        from datetime import datetime, timedelta, timezone
        end = body.window_end or datetime.now(timezone.utc).date()
        start = body.window_start or end - timedelta(days=body.window_days)
        return {"requests": build_discovery_queries(start.isoformat(), end.isoformat(), body.geography, body.accounts, body.qualifications)}

    @app.post("/api/candidates/{id}/confirm")
    def confirm(id: str, body: Confirmation):
        required("candidates", id)
        return confirm_candidate(store, id, **body.model_dump())

    @app.post("/api/candidates/{id}/select")
    def select(id: str):
        required("candidates", id)
        return select_candidate(store, id)

    @app.post("/api/research", status_code=202)
    async def research(body: ResearchInput):
        runs = queue_research(store, body.account_ids, body.web_only)
        spawn(research_worker())
        return {"runs": runs}

    @app.get("/api/accounts/{id}")
    def detail(id: str):
        account = required("accounts", id)
        return public({"account": account, "location_request": build_location_query(account), **{kind: [row for row in store.list(kind) if row.get("account_id") == id]
                      for kind in ("evidence", "agent_runs", "reviews", "signals", "artifacts", "monitors", "deliveries")}})

    @app.post("/api/accounts/{id}/context")
    async def context(id: str, body: ContextInput):
        return public(await read_more_context(store, client, id, body.evidence_id, body.query))

    @app.post("/api/accounts/{id}/reviews")
    def review(id: str, body: ReviewInput):
        with store.transaction() as conn:
            account = store.get("accounts", id, conn=conn)
            if not account:
                raise ValueError("Account not found")
            if body.evidence_id:
                evidence = store.get("evidence", body.evidence_id, conn=conn)
                if not evidence or evidence.get("account_id") != id:
                    raise ValueError("Review evidence must belong to this account")
            record = {"id": new_id("review"), "account_id": id, "created_at": now(), **body.model_dump()}
            store.put("reviews", record, conn=conn)
            account["reviewed_at"] = record["created_at"]
            account["review_decision"] = body.decision
            if body.decision in {"unresolved", "needs_review"}:
                account["needs_recheck"] = True
            store.put("accounts", account, conn=conn)
        return record

    @app.post("/api/accounts/{id}/merge")
    def merge(id: str, body: MergeInput):
        if id == body.target_id:
            raise ValueError("Choose a different target account")
        with store.transaction() as conn:
            source = store.get("accounts", id, conn=conn)
            target = store.get("accounts", body.target_id, conn=conn)
            if not source or not target or source.get("merged_into") or target.get("merged_into"):
                raise ValueError("Choose two active account records")
            if criteria_snapshot(source) != criteria_snapshot(target):
                raise ValueError("Cannot merge accounts with different research criteria or windows")
            if any(r.get("account_id") in {id, body.target_id} and r.get("status") in ACTIVE | {"queued_local"} for r in store.list("agent_runs", conn=conn)):
                raise ValueError("Finish or reconcile active research before merging")
            if any(m.get("account_id") == id for m in store.list("monitors", conn=conn)):
                raise ValueError("Merge into the monitored account, not away from it, to preserve webhook identity")
            target.setdefault("merge_history", []).append({"source": source, "merged_at": now()})
            target["locations"] += source.get("locations", [])
            target["expansion_signals"] += source.get("expansion_signals", [])
            target["needs_recheck"] = True
            for kind in ("evidence", "agent_runs", "artifacts", "signals", "reviews", "candidates"):
                for record in store.list(kind, conn=conn):
                    if record.get("account_id") == id:
                        record.setdefault("original_account_id", id)
                        record["account_id"] = target["id"]
                        store.put(kind, record, conn=conn)
            source["merged_into"] = target["id"]
            store.put("accounts", source, conn=conn)
            store.put("accounts", target, conn=conn)
        return {"account": target, "merged": source}

    @app.get("/api/artifacts/{id}")
    def artifact(id: str):
        return public(required("artifacts", id))

    @app.post("/api/agent-runs/{id}/poll")
    async def poll(id: str):
        required("agent_runs", id)
        result = await poll_account_research(store, client, id)
        if result["status"] in {"queued", "running"}:
            spawn(research_worker())
        return public(result)

    from .monitors import build_router as monitor_router
    from .replay import build_router as replay_router
    app.include_router(monitor_router(store, client, settings))
    app.include_router(replay_router(store, client, settings))
    static = Path(__file__).parent / "static"
    if static.exists():
        app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def index():
        return FileResponse(static / "index.html")

    return app
