"""Verified Monitor deliveries and conservative, field-grounded timeline updates."""
import hashlib
import hmac
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

from .models import REGION_STATES, deterministic_criteria, normalize_url


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def verify_webhook(payload: bytes, signature_header: str, secret: str,
                   tolerance: int = 300, now: float | None = None) -> bool:
    """Retries need a fresh signature within tolerance; never bypass age for duplicates."""
    try:
        parts = [part.strip().split("=", 1) for part in signature_header.split(",")]
        if any(len(part) != 2 for part in parts):
            return False
        timestamps = [v for k, v in parts if k == "t"]
        signatures = [v for k, v in parts if k == "v1"]
        if len(timestamps) != 1 or not signatures or not timestamps[0].isdigit():
            return False
        stamp = timestamps[0]
        if tolerance < 0 or abs((time.time() if now is None else now) - int(stamp)) > tolerance:
            return False
        expected = hmac.new(secret.encode(), stamp.encode() + b"." + payload, hashlib.sha256).hexdigest()
        return any(re.fullmatch(r"[0-9a-fA-F]{64}", sig) and
                   hmac.compare_digest(expected, sig.lower()) for sig in signatures)
    except (TypeError, ValueError, AttributeError):
        return False


def normalized_url(value):
    if not isinstance(value, str):
        return None
    try:
        return normalize_url(value)
    except ValueError:
        return None


def domain(value):
    if not isinstance(value, str):
        return ""
    try:
        return (urlsplit(value if "://" in value else "https://" + value).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def normalize_signals(account, output, discovered_at=None):
    if not isinstance(output, dict) or not isinstance(output.get("results"), (list, type(None))):
        raise ValueError("Malformed monitor output")
    content = output.get("content")
    if content is None and not output.get("results"):
        return []
    if not isinstance(content, dict) or not isinstance(content.get("signals"), list):
        raise ValueError("Expected Monitor output.content.signals")
    grounding = output.get("grounding") or []
    if not isinstance(grounding, list):
        raise ValueError("Malformed grounding")
    normalized = []
    discovery_date = date.fromisoformat((discovered_at or utcnow())[:10])
    for index, item in enumerate(content["signals"]):
        if not isinstance(item, dict):
            raise ValueError("Malformed signal")
        for key in ("company", "domain", "type", "description", "location", "eventDate", "publicationDate", "eventDateStatus", "publicationDateStatus"):
            if key not in item or (item[key] is not None and not isinstance(item[key], str)):
                raise ValueError(f"Malformed signal field: {key}")
        fields = {}
        for entry in grounding:
            if not isinstance(entry, dict):
                continue
            if entry.get("confidence") == "low":
                continue
            field = str(entry.get("field", "")).removeprefix("$.").lstrip("/").replace("/", ".").removeprefix("content.")
            field = re.sub(r"\[(\d+)\]", r".\1", field)
            prefix = f"signals.{index}."
            if field.startswith(prefix):
                citations = entry.get("citations") or []
                fields[field[len(prefix):]] = [c for c in citations if isinstance(c, dict) and normalized_url(c.get("url"))]
        reasons = []
        if not item.get("type") or not item.get("description"):
            reasons.append("Signal type or description unknown")
        if not account.get("domain_verified") or domain(item.get("domain")) != domain(account.get("domain")):
            reasons.append("Company domain mismatch or unverified identity")
        if str(item.get("company", "")).strip().casefold() != str(account.get("name", "")).strip().casefold():
            reasons.append("Company name requires identity review")
        location = item.get("location")
        geography = account.get("geography", "Texas")
        states = REGION_STATES.get(geography, set())
        # Match a state suffix, not street names, Washington DC, or the word "or".
        state_pattern = "|".join(state.upper() if len(state) == 2 else f"(?i:{state})" for state in sorted(states))
        location_pattern = rf"(?:^|[\s,])(?:{state_pattern})(?:\s+\d{{5}}(?:-\d{{4}})?)?(?:,?\s+(?i:USA|US|United States))?\s*$"
        if geography == "Texas":
            location_pattern = r"\b(?i:Texas|TX)\b"  # Preserve validation for historical deliveries and replay.
        if not deterministic_criteria(account):
            reasons.append("Custom geography and qualifications require human review against saved criteria")
        elif not states or not isinstance(location, str) or not re.search(location_pattern, location):
            reasons.append(f"{geography} location not established")
        for key in ("company", "domain", "type", "description", "location", "eventDate", "publicationDate"):
            if not fields.get(key):
                reasons.append(f"Missing field grounding: {key}")
        dates = {}
        for key in ("eventDate", "publicationDate"):
            try:
                value = item.get(key)
                if not isinstance(value, str) or len(value) != 10:
                    raise ValueError()
                dates[key] = date.fromisoformat(value).isoformat()
                if item.get(key + "Status") not in {"known", "confirmed", "actual", "announced", "planned"}:
                    reasons.append(f"Uncertain date status: {key}")
            except ValueError:
                dates[key] = None
                reasons.append(f"Unknown or invalid date: {key}")
        days = account.get("window_days", 90)
        if dates["eventDate"] and not ((discovery_date - timedelta(days=days)).isoformat() <= dates["eventDate"] <= discovery_date.isoformat()):
            reasons.append(f"Event outside monitoring {days}-day window")
        if dates["publicationDate"] and dates["publicationDate"] > (discovered_at or utcnow())[:10]:
            reasons.append("Publication date is in the future")
        citations = {normalized_url(c["url"]): c for values in fields.values() for c in values}
        source = sorted(citations)[0] if citations else ""
        key = [account["id"], source, item.get("type"), dates["eventDate"], str(location or "").strip().casefold()]
        # Ungrounded findings still need distinct review identities, not a shared empty key.
        if not source:
            key.append(item.get("description"))
        signal_id = "signal_" + hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:24]
        normalized.append({"id": signal_id, "account_id": account["id"],
                           "company": item.get("company"), "domain": item.get("domain"),
                           "type": item.get("type"), "description": item.get("description"),
                           "location": location, "event_date": dates["eventDate"],
                           "publication_date": dates["publicationDate"],
                           "event_date_status": item.get("eventDateStatus"),
                           "publication_date_status": item.get("publicationDateStatus"),
                           "discovered_at": discovered_at or utcnow(), "source_url": source,
                           "citations": list(citations.values()), "field_grounding": fields,
                           "status": "needs_review" if reasons else "supported", "review_reasons": reasons})
    return normalized


def validate_event(event, monitor):
    if not isinstance(event, dict) or not isinstance(event.get("id"), str) or not event["id"]:
        raise ValueError("Missing event identity")
    if event.get("type") != "monitor.run.completed":
        raise ValueError("Unsupported event type")
    run = event.get("data")
    if not isinstance(run, dict) or not isinstance(run.get("id"), str) or not run["id"]:
        raise ValueError("Missing run identity")
    if run.get("monitorId") != monitor.get("remote_id"):
        raise ValueError("Wrong monitor identity")
    metadata = run.get("metadata")
    if metadata is not None and (not isinstance(metadata, dict) or metadata.get("account_id") != monitor["account_id"] or metadata.get("project") != "account-watch"):
        raise ValueError("Wrong account metadata")
    if run.get("status") not in {"completed", "failed", "cancelled"}:
        raise ValueError("Run is not terminal")
    return run


def persist_delivery(store, monitor, event):
    """Commit the verified inbox before attempting the application transaction."""
    run = validate_event(event, monitor)
    with store.transaction() as conn:
        existing = store.get("deliveries", event["id"], conn=conn)
        if existing:
            if existing.get("payload") != event:
                raise ValueError("Event ID reused with different payload")
            return existing
        record = {"id": event["id"], "account_id": monitor["account_id"],
                  "monitor_id": monitor["remote_id"], "run_id": run["id"],
                  "verified": True, "received_at": utcnow(), "status": "pending", "payload": event}
        store.put("deliveries", record, conn=conn)
        return record


def apply_monitor_update(store, delivery_id):
    with store.transaction() as conn:
        delivery = store.get("deliveries", delivery_id, conn=conn)
        if not delivery or not delivery.get("verified"):
            raise ValueError("A verified saved delivery is required")
        if delivery.get("status") in {"applied", "duplicate"}:
            return {"status": "duplicate", "added": 0, "delivery_id": delivery_id}
        monitor = store.get("monitors", delivery["account_id"], conn=conn)
        run = validate_event(delivery["payload"], monitor)
        previous = next((d for d in store.list("deliveries", conn=conn)
                         if d["id"] != delivery_id and d.get("monitor_id") == delivery["monitor_id"]
                         and d.get("run_id") == delivery["run_id"] and d.get("status") == "applied"), None)
        if previous:
            delivery.update(status="duplicate", duplicate_of=previous["id"])
            store.put("deliveries", delivery, conn=conn)
            return {"status": "duplicate", "added": 0, "delivery_id": delivery_id}
        account = store.get("accounts", delivery["account_id"], conn=conn)
        if not account:
            raise ValueError("Account not found")
        snapshot = {"accounts": [account], "monitors": [monitor]}
        for kind in ("signals", "evidence", "reviews"):
            snapshot[kind] = [r for r in store.list(kind, conn=conn) if r.get("account_id") == account["id"]]
        snapshot["sessions"] = [r for r in store.list("sessions", conn=conn) if r["id"] == account.get("session_id")]
        delivery["pre_update"] = snapshot
        if run["status"] != "completed":
            delivery.update(status="error", error=run.get("failReason") or run["status"])
            monitor.update(last_error=delivery["error"], last_received_update=delivery["received_at"])
            store.put("monitors", monitor, conn=conn)
            store.put("deliveries", delivery, conn=conn)
            return {"status": "error", "added": 0, "delivery_id": delivery_id}
        signals = normalize_signals(account, run.get("output"), delivery["received_at"])
        added = 0
        for signal in signals:
            if not store.get("signals", signal["id"], conn=conn):
                signal["delivery_id"] = delivery_id
                store.put("signals", signal, conn=conn)
                for citation in signal["citations"]:
                    evidence_id = "evidence_" + hashlib.sha256((signal["id"] + citation["url"]).encode()).hexdigest()[:24]
                    store.put("evidence", {"id": evidence_id, "account_id": account["id"], "url": citation["url"], "title": citation.get("title"), "signal_id": signal["id"], "source": "monitor"}, conn=conn)
                added += 1
        if added:
            account["needs_recheck"] = True
        account["last_checked"] = run.get("completedAt") or delivery["received_at"]
        account["last_received_update"] = delivery["received_at"]
        monitor.update(last_checked=account["last_checked"], last_received_update=delivery["received_at"], last_result="New findings" if added else "No new findings", last_error=None)
        delivery.update(status="applied", applied_at=utcnow(), added=added)
        for kind, record in (("accounts", account), ("monitors", monitor), ("deliveries", delivery)):
            store.put(kind, record, conn=conn)
        return {"status": "applied", "added": added, "delivery_id": delivery_id}
