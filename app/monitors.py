"""Explicit manual Monitor actions; no scheduled or paid work at startup."""
import ipaddress
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit, quote, urlencode

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, StrictBool

from .api import sanitize
from .updates import utcnow
from .models import criteria_snapshot, deterministic_criteria


def webhook_url(receiver_url, token):
    parsed = urlsplit(receiver_url or "")
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("A public HTTPS receiver URL is required")
    if parsed.hostname == "localhost" or parsed.hostname.endswith((".local", ".localhost")):
        raise ValueError("Receiver must be public")
    try:
        if not ipaddress.ip_address(parsed.hostname).is_global:
            raise ValueError("Receiver must be public")
    except ValueError as exc:
        if str(exc) == "Receiver must be public":
            raise
    return receiver_url.rstrip("/") + "/webhooks/exa/" + token


def save_secret(path, token, secret):
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise ValueError("Secrets directory must not be a symlink")
    os.chmod(directory, 0o700)
    fd = os.open(directory / token, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(secret)
        stream.flush()
        os.fsync(stream.fileno())


def read_secret(path, token):
    if not token or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in token):
        raise ValueError("Invalid monitor token")
    fd = os.open(Path(path) / token, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        if os.fstat(stream.fileno()).st_mode & 0o077:
            raise ValueError("Secret permissions must be 0600")
        return stream.read()


def build_monitor_request(account, receiver_url, local_token):
    if not (account.get("domain_verified") or account.get("domain_accepted")) or not account.get("domain"):
        raise ValueError("Add a source-backed company domain first")
    fields = ("company", "domain", "type", "description", "location", "eventDate", "publicationDate", "eventDateStatus", "publicationDateStatus")
    return {"name": f"Account Watch {account['id']}: {account['name']}",
            "search": {"query": f"Monitor developments involving exactly {account['name']} ({account['domain']}). Saved research criteria: {criteria_snapshot(account)}. Look for new evidence relevant to those qualifications in that geography and account type. Monitoring uses a rolling {account.get('window_days', 90)}-day window; the saved research dates are the historical baseline, not the monitoring cutoff. Verify operating identity. Distinguish announcement, planned and actual dates. " + ("For locations, end with the US state name or abbreviation." if deterministic_criteria(account) else "Preserve full location and country; custom-criteria signals require human review."), "numResults": 5, "contents": {"highlights": True}},
            "metadata": {"account_id": account["id"], "project": "account-watch"},
            "webhook": {"url": webhook_url(receiver_url, local_token), "events": ["monitor.run.completed"]},
            "outputSchema": {"type": "object", "properties": {"signals": {"type": "array", "items": {"type": "object", "properties": {key: {"type": ["string", "null"]} for key in fields}, "required": list(fields)}}}, "required": ["signals"]}}


async def create_account_monitor(store, client, settings, account_id):
    with store.transaction() as conn:
        account = store.get("accounts", account_id, conn=conn)
        if not account:
            raise ValueError("Account not found")
        existing = store.get("monitors", account_id, conn=conn)
        if existing:
            if existing.get("remote_id") and existing.get("status") not in {"creating", "creation_uncertain"}:
                return existing
            raise ValueError("Creation outcome uncertain: reconcile in Exa before retrying; no duplicate create sent")
        token = secrets.token_urlsafe(32)
        body = build_monitor_request(account, settings.receiver_url, token)
        record = {"id": account_id, "account_id": account_id, "local_token": token,
                   "status": "creating", "created_at": utcnow(), "request": body, "criteria": criteria_snapshot(account)}
        store.put("monitors", record, conn=conn)
    try:
        result = await client.request("POST", "/monitors", body, account_id=account_id, operation="create_monitor")
        record["remote_id"] = result["id"]
        store.put("monitors", record)
        save_secret(settings.secrets_path, token, result["webhookSecret"])
        record["secret_available"] = True
        record.update(status=result["status"], trigger=result.get("trigger"), next_run_at=result.get("nextRunAt"), webhook=result.get("webhook"))
    except Exception:
        record["status"] = "creation_uncertain"
        store.put("monitors", record)
        raise
    store.put("monitors", record)
    return record


async def reconcile_monitor(store, client, settings, account_id):
    monitor = store.get("monitors", account_id)
    if not monitor:
        raise ValueError("No monitor creation to reconcile")
    query = {"metadata[account_id]": account_id, "metadata[project]": "account-watch", "limit": 100}
    matches = []
    cursors = set()
    while True:
        response = await client.request("GET", "/monitors?" + urlencode(query), account_id=account_id, operation="reconcile_monitor")
        if not isinstance(response.get("data"), list):
            raise ValueError("Malformed monitor list; reservation retained")
        matches.extend(m for m in response["data"] if m.get("metadata") == {"account_id": account_id, "project": "account-watch"})
        if response.get("hasMore") is False:
            break
        cursor = response.get("nextCursor")
        if not cursor or cursor in cursors or len(cursors) >= 100:
            raise ValueError("Incomplete monitor listing; reservation retained")
        cursors.add(cursor)
        query["cursor"] = cursor
    if len(matches) != 1:
        monitor.update(status="creation_uncertain", last_error="No matching remote monitor found; creation may still be in flight. Reservation retained." if not matches else "Multiple remote monitors match; resolve duplicates in Exa before continuing.")
    else:
        remote = matches[0]
        monitor.update(remote_id=remote["id"], status=remote["status"], trigger=remote.get("trigger"), next_run_at=remote.get("nextRunAt"), webhook=remote.get("webhook"))
        try:
            read_secret(settings.secrets_path, monitor["local_token"])
            monitor["secret_available"] = True
            monitor["last_error"] = None
        except (OSError, ValueError):
            monitor.update(status="secret_unavailable", secret_available=False, last_error="Remote monitor found but one-time signing secret is unavailable. Do not enable checks; resolve signing configuration with Exa. Pause remains available.")
    store.put("monitors", monitor)
    return monitor


def get_monitor(store, account_id, require_secret=True):
    monitor = store.get("monitors", account_id)
    if not monitor or not monitor.get("remote_id") or monitor.get("status") in {"creating", "creation_uncertain"}:
        raise ValueError("Create or reconcile this account's monitor first")
    if require_secret and (monitor.get("status") == "secret_unavailable" or monitor.get("secret_available") is False):
        raise ValueError("Monitor signing secret unavailable; reconcile before enabling checks")
    return monitor


async def trigger_check(store, client, account_id):
    monitor = get_monitor(store, account_id)
    result = await client.request("POST", f"/monitors/{quote(monitor['remote_id'], safe='')}/trigger", account_id=account_id, operation="trigger_monitor")
    monitor.update(last_triggered_at=utcnow(), last_trigger_response=sanitize(result))
    store.put("monitors", monitor)
    return result


async def set_monitor_schedule(store, client, account_id, enabled):
    monitor = get_monitor(store, account_id, require_secret=enabled)
    body = {"status": "active", "trigger": {"type": "interval", "period": "1d"}} if enabled else {"status": "paused"}
    result = await client.request("PATCH", f"/monitors/{quote(monitor['remote_id'], safe='')}", body, account_id=account_id, operation="schedule_monitor")
    monitor.update(status=result["status"], trigger=result.get("trigger"), next_run_at=result.get("nextRunAt"))
    store.put("monitors", monitor)
    return monitor


class ScheduleBody(BaseModel):
    enabled: StrictBool


class WebhookBody(BaseModel):
    receiver_url: str


def build_router(store, client, settings):
    router = APIRouter()

    @router.get("/api/accounts/{account_id}/monitor/preview")
    def preview(account_id: str):
        account = store.get("accounts", account_id)
        if not account:
            raise HTTPException(404, "Account not found")
        body = build_monitor_request(account, settings.receiver_url or "https://receiver.example.com", "assigned-on-create")
        return {"request": body, "receiver_configured": bool(settings.receiver_url),
                "note": "Preview only. The webhook path token is assigned on creation; no remote monitor or schedule was created."}

    async def invoke(function, *args):
        try:
            return sanitize(await function(*args))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/api/accounts/{account_id}/monitor")
    async def create(account_id: str):
        return await invoke(create_account_monitor, store, client, settings, account_id)

    @router.post("/api/accounts/{account_id}/monitor/trigger")
    async def trigger(account_id: str):
        return await invoke(trigger_check, store, client, account_id)

    @router.post("/api/accounts/{account_id}/monitor/reconcile")
    async def reconcile(account_id: str):
        return await invoke(reconcile_monitor, store, client, settings, account_id)

    @router.post("/api/accounts/{account_id}/monitor/schedule")
    async def schedule(account_id: str, body: ScheduleBody):
        return await invoke(set_monitor_schedule, store, client, account_id, body.enabled)

    @router.patch("/api/accounts/{account_id}/monitor/webhook")
    async def update_webhook(account_id: str, body: WebhookBody):
        try:
            monitor = get_monitor(store, account_id)
            url = webhook_url(body.receiver_url, monitor["local_token"])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        result = await client.request("PATCH", f"/monitors/{quote(monitor['remote_id'], safe='')}", {"webhook": {"url": url}}, account_id=account_id, operation="update_webhook")
        monitor["webhook"] = result.get("webhook")
        monitor["request"]["webhook"]["url"] = url
        store.put("monitors", monitor)
        return sanitize(monitor)

    @router.get("/api/accounts/{account_id}/monitor/runs")
    @router.get("/api/accounts/{account_id}/monitor/runs/{run_id}")
    async def runs(account_id: str, run_id: str | None = None):
        try:
            monitor = get_monitor(store, account_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        path = f"/monitors/{quote(monitor['remote_id'], safe='')}/runs"
        path += "/" + quote(run_id, safe="") if run_id else "?limit=20"
        return sanitize(await client.request("GET", path, account_id=account_id, operation="monitor_runs"))

    return router
