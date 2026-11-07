from .api import sanitize
from .models import new_id, normalize_url, now


async def read_more_context(store, client, account_id, evidence_id, query="Clarify whether this location is operating or planned"):
    with store.transaction() as conn:
        account = store.get("accounts", account_id, conn=conn)
        evidence = store.get("evidence", evidence_id, conn=conn)
        if not account or not evidence or evidence.get("account_id") != account_id:
            raise ValueError("Choose a source already associated with this account")
        normalize_url(evidence["url"])
        if account.get("context_reads", 0) >= 2:
            raise ValueError("At most two extra Contents reads per account")
        account["context_reads"] = account.get("context_reads", 0) + 1
        store.put("accounts", account, conn=conn)
        context = {"id": new_id("evidence"), "account_id": account_id, "session_id": account["session_id"],
                   "parent_evidence_id": evidence_id, "url": evidence["url"], "title": evidence.get("title"),
                   "previous_passage": evidence.get("highlights", []), "query": query,
                   "source": "contents", "status": "pending", "discovered_at": now()}
        store.put("evidence", context, conn=conn)
    try:
        response = await client.request("POST", "/contents", {"ids": [evidence["url"]], "text": True,
                                        "highlights": {"query": query}}, account_id=account_id, operation="contents")
        context["raw"] = sanitize(response)
        status = next((item for item in response.get("statuses", []) if item.get("id") == evidence["url"]), None)
        result = next((item for item in response.get("results", []) if item.get("url") == evidence["url"] or item.get("id") == evidence["url"]), None)
        if not status or status.get("status") != "success" or not result:
            context.update(status="failed", error="Per-URL Contents status did not establish success")
        else:
            context.update(status="needs_review", highlights=result.get("highlights", []), text=result.get("text"), title=result.get("title"))
    except Exception as exc:
        context.update(status="failed", error=f"Contents request failed ({type(exc).__name__}); original passage preserved")
    return store.put("evidence", context)
