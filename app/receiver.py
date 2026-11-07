"""Public receiver: health and authenticated webhook only, no private UI routes."""
import json
import hashlib
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request

from .config import load_settings
from .monitors import read_secret
from .store import Store
from .updates import apply_monitor_update, persist_delivery, verify_webhook


def create_app(settings=None, store=None):
    settings = settings or load_settings()
    store = store or Store(settings.db_path)
    @asynccontextmanager
    async def lifespan(app):
        for delivery in store.list("deliveries"):
            if delivery.get("status") == "pending" and delivery.get("verified"):
                try:
                    apply_monitor_update(store, delivery["id"])
                except ValueError as exc:
                    delivery.update(status="error", error=str(exc))
                    store.put("deliveries", delivery)
        yield

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/webhooks/exa/{local_monitor_token}")
    async def webhook(local_monitor_token: str, request: Request):
        monitor = next((m for m in store.list("monitors") if m.get("local_token") == local_monitor_token), None)
        if not monitor:
            raise HTTPException(404, "Unknown monitor")
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 2_000_000:
                raise HTTPException(413, "Delivery too large")
            raw.extend(chunk)
        raw = bytes(raw)
        try:
            secret = read_secret(settings.secrets_path, local_monitor_token)
        except (OSError, ValueError):
            raise HTTPException(503, "Monitor signing secret unavailable")
        public_config = store.get("config", "public") or {}
        tolerance = public_config.get("signature_tolerance", settings.signature_tolerance)
        if not isinstance(tolerance, int) or isinstance(tolerance, bool) or not 1 <= tolerance <= 86400:
            raise HTTPException(503, "Invalid signature tolerance configuration")
        if not verify_webhook(raw, request.headers.get("exa-signature", ""), secret, tolerance):
            raise HTTPException(401, "Invalid or expired signature")
        try:
            event = json.loads(raw)
            delivery = persist_delivery(store, monitor, event)
        except (ValueError, TypeError) as exc:
            store.put("deliveries", {"id": "invalid_" + hashlib.sha256(raw).hexdigest(),
                      "account_id": monitor["account_id"], "verified": True,
                      "status": "error", "error": str(exc)})
            raise HTTPException(400, str(exc)) from exc
        try:
            return apply_monitor_update(store, delivery["id"])
        except ValueError as exc:
            with store.transaction() as conn:
                current = store.get("deliveries", delivery["id"], conn=conn)
                current.update(status="error", error=str(exc))
                store.put("deliveries", current, conn=conn)
            return {"status": "error", "delivery_id": delivery["id"], "error": str(exc)}

    return app
