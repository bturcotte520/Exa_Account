"""Sanitized read-only recordings and isolated replay of saved deliveries."""
import copy
import csv
import io
import json
import re
import tempfile
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from .store import Store
from .api import sanitize
from .updates import apply_monitor_update, utcnow
from .models import criteria_snapshot


KINDS = ("sessions", "candidates", "accounts", "evidence", "agent_runs", "artifacts", "monitors", "deliveries", "signals", "reviews")


def sanitize_export(value, *, request=False):
    """No redistribution rights are assumed, even for excerpts or partner enrichment."""
    blocked = {"apikey", "exakey", "jevkey", "findata", "authorization", "xapikey", "webhooksecret", "secret", "signature", "exasignature", "localtoken", "rawbody", "text", "html", "body", "pagetext", "fulltext", "highlights", "excerpt", "passage", "context", "employeecount", "headcount", "employeerange", "partnerdata", "fiberdata", "contacts", "people"}
    if isinstance(value, list):
        return [sanitize_export(item, request=request) for item in value]
    if isinstance(value, dict):
        provider = str(value.get("provider", value.get("source", ""))).lower()
        if provider == "fiber" and set(value) - {"provider", "source", "status", "requested"}:
            return {"provider": "fiber", "redistribution": "omitted; rights not established"}
        result = {}
        for key, item in value.items():
            compact = key.replace("_", "").replace("-", "").lower()
            request_option = request and compact in {"body", "text", "highlights"} and isinstance(item, (dict, bool))
            source_passage = any(word in compact for word in ("passage", "excerpt", "highlight"))
            if ((compact in blocked or source_passage) and not request_option) or "secret" in compact or "password" in compact or "token" in compact:
                continue
            if key == "task" and isinstance(item, str):
                result[key] = "Omitted: task may embed source excerpts without redistribution rights"
                continue
            if key == "query" and isinstance(item, str):
                item = re.sub(r"<evidence>.*</evidence>",
                              "<evidence>Omitted: source redistribution rights not established</evidence>",
                              item, flags=re.DOTALL)
            if key == "url" and isinstance(item, str) and "/webhooks/exa/" in item:
                continue
            result[key] = sanitize_export(item, request=request or key == "request")
        return result
    return sanitize(value)


def export_recording(store):
    return {"format": "account-watch", "version": 1, "recorded_at": utcnow(),
            "label": "Recorded session", "read_only": True,
            "redistribution": "Page bodies, excerpts and partner enrichment omitted; rights not established",
            "records": sanitize_export({kind: store.list(kind) for kind in KINDS})}


def validate_recording(recording):
    if not isinstance(recording, dict) or recording.get("format") != "account-watch" or recording.get("version") != 1:
        raise ValueError("Unsupported recording format/version")
    if not isinstance(recording.get("recorded_at"), str) or not isinstance(recording.get("records"), dict):
        raise ValueError("Recording date and records required")
    try:
        datetime.fromisoformat(recording["recorded_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid recording date") from exc
    if len(json.dumps(recording)) > 20_000_000:
        raise ValueError("Recording exceeds 20 MB")
    for kind, records in recording["records"].items():
        if kind not in KINDS or not isinstance(records, list):
            raise ValueError("Unknown record kind or invalid collection")
        seen = set()
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"]:
                raise ValueError("Every recorded item needs a string id")
            if record["id"] in seen:
                raise ValueError("Duplicate recording record id")
            seen.add(record["id"])
    cleaned = sanitize_export(recording)
    cleaned.update(read_only=True, label="Synthetic test" if recording.get("synthetic") is True else "Recorded session")
    return cleaned


def import_recording(store, recording):
    snapshot = validate_recording(recording)
    record = {"id": "recording_" + uuid4().hex, "created_at": utcnow(), "recorded_at": snapshot["recorded_at"],
              "read_only": True, "label": snapshot["label"], "recording": snapshot}
    store.put("recordings", record)
    return record


def replay_delivery(store, delivery_id, recording_id=None):
    synthetic_recording = False
    if recording_id:
        recording = store.get("recordings", recording_id)
        if not recording:
            raise ValueError("Recording not found")
        synthetic_recording = recording["recording"].get("synthetic") is True or recording.get("label") == "Synthetic test"
        delivery = next((d for d in recording["recording"]["records"].get("deliveries", []) if d["id"] == delivery_id), None)
    else:
        delivery = store.get("deliveries", delivery_id)
    if not delivery or not delivery.get("verified") or not delivery.get("pre_update"):
        raise ValueError("Replay requires a verified saved payload and its pre-update snapshot")
    with tempfile.TemporaryDirectory(prefix="account-watch-replay-") as directory:
        sandbox = Store(Path(directory) / "sandbox.sqlite")
        snapshot = delivery["pre_update"]
        for kind in ("accounts", "monitors", "signals", "evidence", "reviews", "sessions"):
            for record in snapshot.get(kind, []):
                sandbox.put(kind, copy.deepcopy(record))
        replay = copy.deepcopy(delivery)
        replay.pop("pre_update", None)
        replay.update(status="pending")
        sandbox.put("deliveries", replay)
        def counts():
            return {"accounts": len(sandbox.list("accounts")), "signals": len(sandbox.list("signals"))}
        before = counts()
        first = apply_monitor_update(sandbox, delivery_id)
        after_first = counts()
        second = apply_monitor_update(sandbox, delivery_id)
        return {"label": "Synthetic test" if delivery.get("synthetic") or synthetic_recording else "Replay of recorded delivery",
                "isolated": True, "before": before, "first": first, "after_first": after_first,
                "verification_basis": "Imported recording claims prior verification; signature not revalidated" if recording_id else "Locally verified delivery",
                "second": second, "after_second": counts(),
                "account": sanitize_export(sandbox.get("accounts", delivery["account_id"]))}


def export_csv(store):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=["id", "name", "domain", "geography", "accounts", "qualifications", "window_days", "window_start", "window_end", "fit", "fit_explanation", "sources"])
    writer.writeheader()
    evidence = store.list("evidence")
    for account in store.list("accounts"):
        if account.get("merged_into"):
            continue
        row = {field: account.get(field, "") for field in writer.fieldnames if field != "sources"}
        row.update(criteria_snapshot(account))
        row["sources"] = " | ".join(sorted({e["url"] for e in evidence if e.get("account_id") == account["id"] and isinstance(e.get("url"), str)}))
        # Spreadsheet formula injection is possible in names and fetched source metadata.
        writer.writerow({key: "'" + str(value) if str(value).startswith(("=", "+", "-", "@", "\t", "\r")) else value for key, value in row.items()})
    return stream.getvalue()


class OpenRecordingBody(BaseModel):
    recording: dict


def build_router(store, client, settings):
    router = APIRouter()

    @router.get("/api/export/json")
    def export_json():
        return export_recording(store)

    @router.get("/api/export/csv")
    def csv_export():
        return Response(export_csv(store), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="account-watch.csv"'})

    @router.post("/api/recordings/save")
    def save():
        return import_recording(store, export_recording(store))

    @router.post("/api/recordings/open")
    def open_recording(body: OpenRecordingBody):
        try:
            return import_recording(store, body.recording)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/api/replay/{delivery_id}")
    def replay(delivery_id: str, recording_id: str | None = None):
        try:
            return replay_delivery(store, delivery_id, recording_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    return router
