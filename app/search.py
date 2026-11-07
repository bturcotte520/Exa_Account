from datetime import datetime, timedelta, timezone

from .api import sanitize
from .models import ACCOUNTS, QUALIFICATIONS, Criteria, GEOGRAPHY, criteria_snapshot, new_id, normalize_domain, normalize_url, now, stable_id


DIRECTORY_DOMAINS = {"linkedin.com", "facebook.com", "instagram.com", "yelp.com", "google.com", "mapquest.com", "yellowpages.com", "franchise.org", "franchising.com"}


def create_session(store, criteria=None):
    criteria = criteria or Criteria()
    end = datetime.now(timezone.utc)
    return store.put("sessions", {"id": new_id("session"), "created_at": end.isoformat(),
                                 **criteria.model_dump(mode="json"),
                                 "window_mode": "dates" if criteria.window_start else "days",
                                 "window_start": (criteria.window_start or (end - timedelta(days=criteria.window_days)).date()).isoformat(),
                                 "window_end": (criteria.window_end or end.date()).isoformat(),
                                 "status": "queued", "searches_started": 0})


def build_discovery_queries(window_start, window_end, geography=GEOGRAPHY, accounts=ACCOUNTS, qualifications=QUALIFICATIONS):
    common = {"type": "auto", "numResults": 10, "contents": {"highlights": True}}
    return [
        {**common, "category": "company", "query": f"Find {accounts} in {geography}. Criteria: {qualifications}. Official company pages; identify the operating company versus franchise brand"},
        {**common, "query": f"Find expansion announcements, new locations and openings for {accounts} in {geography}, qualifications: {qualifications}, between {window_start} and {window_end}; identify the operating company",
         "startPublishedDate": window_start + "T00:00:00.000Z", "endPublishedDate": window_end + "T23:59:59.999Z"},
    ]


async def discover_companies(store, client, session_id):
    with store.transaction() as conn:
        session = store.get("sessions", session_id, conn=conn)
        if not session or session["status"] != "queued":
            raise ValueError("Discovery has already started; create an explicit new session")
        session["status"] = "running"
        store.put("sessions", session, conn=conn)
    candidates = {}
    try:
        for index, body in enumerate(build_discovery_queries(session["window_start"], session["window_end"], session.get("geography", "Texas"), session.get("accounts", ACCOUNTS), session.get("qualifications", QUALIFICATIONS))):
            session["searches_started"] = index + 1
            store.put("sessions", session)
            response = await client.request("POST", "/search", body, operation="discovery")
            for rank, result in enumerate(response.get("results", []), 1):
                try:
                    url = normalize_url(result["url"])
                    domain = normalize_domain(url)
                except (ValueError, KeyError, TypeError):
                    continue
                evidence_id = stable_id("evidence", session_id, url)
                previous = store.get("evidence", evidence_id)
                evidence = previous or {"id": evidence_id, "session_id": session_id, "account_id": None,
                                       "url": result["url"], "title": result.get("title"),
                                       "highlights": result.get("highlights", []), "publication_date": result.get("publishedDate"),
                                       "discovered_at": now(), "source": "search", "raw": sanitize(result), "appearances": []}
                evidence.update(query_id=response.get("requestId"), rank=rank)
                evidence["appearances"].append({"query_id": response.get("requestId"), "rank": rank, "query_index": index})
                store.put("evidence", evidence)
                if domain not in candidates and len(candidates) < 10:
                    candidates[domain] = {"id": stable_id("candidate", session_id, domain), "session_id": session_id,
                                          "name": result.get("title") or domain, "domain": domain,
                                          "domain_verified": False, "selected": False, "account_id": None,
                                          "status": "candidate", "evidence_ids": [],
                                          "entity_note": "Domain from Search result, not entity-verified. Agent research must establish operating identity."}
                if domain in candidates:
                    candidate = candidates[domain]
                    if evidence_id not in candidate["evidence_ids"]:
                        candidate["evidence_ids"].append(evidence_id)
                    store.put("candidates", candidate)
        session["status"] = "completed"
    except Exception as exc:
        session.update(status="failed", error=f"Discovery stopped: {type(exc).__name__}. Inspect requests; no automatic retry.")
    return store.put("sessions", session)


def confirm_candidate(store, candidate_id, domain, name=None, note=""):
    domain = normalize_domain(domain)
    if any(domain == host or domain.endswith("." + host) for host in DIRECTORY_DOMAINS):
        raise ValueError("A directory or social profile is not an official company domain")
    with store.transaction() as conn:
        candidate = store.get("candidates", candidate_id, conn=conn)
        if candidate is None:
            raise ValueError("Candidate not found")
        if candidate.get("selected"):
            raise ValueError("Selected identity is immutable; use Merge duplicate")
        candidate.update(domain=domain, name=name or candidate["name"], domain_verified=True,
                         confirmation_note=note, confirmed_at=now(), status="confirmed")
        return store.put("candidates", candidate, conn=conn)


def select_candidate(store, candidate_id):
    with store.transaction() as conn:
        candidate = store.get("candidates", candidate_id, conn=conn)
        if not candidate:
            raise ValueError("Candidate not found")
        domain = normalize_domain(candidate["domain"])
        if any(domain == host or domain.endswith("." + host) for host in DIRECTORY_DOMAINS):
            raise ValueError("A directory or social profile is not a company domain; choose an official company result")
        evidence = [store.get("evidence", eid, conn=conn) for eid in candidate.get("evidence_ids", [])]
        if not candidate.get("domain_verified") and not any(e and e.get("session_id") == candidate["session_id"] and normalize_domain(e.get("url", "")) == domain for e in evidence):
            raise ValueError("Candidate domain must have saved source evidence")
        candidate.update(domain=domain, domain_accepted=True, accepted_at=now())
        selected = [row for row in store.list("candidates", conn=conn) if row.get("selected") and row["session_id"] == candidate["session_id"]]
        if not candidate.get("selected") and len(selected) >= 5:
            raise ValueError("At most five candidates may be selected per session")
        session = store.get("sessions", candidate["session_id"], conn=conn)
        geography = session.get("geography", "Texas")
        snapshot = criteria_snapshot(session)
        account_id = stable_id("account", candidate["domain"], session["id"])
        account = store.get("accounts", candidate.get("account_id") or account_id, conn=conn)
        visited = set()
        while account and account.get("merged_into"):
            if account["id"] in visited:
                raise ValueError("Account merge history contains a cycle")
            visited.add(account["id"])
            account = store.get("accounts", account["merged_into"], conn=conn)
            if account is None:
                raise ValueError("Merged account target is missing")
            if account.get("geography", "Texas") != geography:
                raise ValueError("Merged account has a different research geography")
        if account is None:
            account = {"id": account_id, "name": candidate["name"], "domain": candidate["domain"], "domain_verified": bool(candidate.get("domain_verified")),
                       "domain_accepted": True, "entity_verification_status": "unclear", "identity_basis": "Accepted candidate source domain; operating entity requires grounded research", **snapshot,
                       "session_id": session["id"], "window_start": session["window_start"], "window_end": session["window_end"],
                       "geography": geography, "fit": "needs_review", "fit_explanation": "Not researched",
                       "locations": [], "expansion_signals": [], "employee_count": None,
                       "partner_requested": False, "provenance": {}, "needs_recheck": False, "last_researched": None}
            store.put("accounts", account, conn=conn)
        candidate.update(selected=True, account_id=account["id"], status="selected")
        store.put("candidates", candidate, conn=conn)
        for evidence_id in candidate["evidence_ids"]:
            evidence = store.get("evidence", evidence_id, conn=conn)
            evidence["account_id"] = account["id"]
            store.put("evidence", evidence, conn=conn)
        return {"candidate": candidate, "account": account}


def build_location_query(account):
    # Places is automatically included for place queries; text must be fetched inline.
    return {"query": f"Physical locations of {account['name']} ({account['domain']}) in {account.get('geography', 'Texas')}: street addresses, local business listings, opening hours and operating company. Distinguish operating branches from planned or announced locations and unrelated franchisees.",
            "type": "auto", "numResults": 5, "contents": {"text": True, "highlights": True}}


async def search_locations(store, client, account):
    with store.transaction() as conn:
        current = store.get("accounts", account["id"], conn=conn)
        if current.get("location_search_status") == "completed":
            return [row for row in store.list("evidence", conn=conn) if row.get("account_id") == account["id"] and row.get("purpose") == "locations"]
        if current.get("location_search_status"):
            raise ValueError("Location search was already attempted; reconcile its artifact before another paid request")
        current["location_search_status"] = "started"
        store.put("accounts", current, conn=conn)
    response = await client.request("POST", "/search", build_location_query(account), account_id=account["id"], operation="location_search")
    evidence = []
    for rank, result in enumerate(response.get("results", []), 1):
        try:
            url = normalize_url(result["url"])
        except (KeyError, TypeError, ValueError):
            continue
        record = {"id": stable_id("evidence", account["id"], "locations", url), "account_id": account["id"],
                  "session_id": account["session_id"], "url": result["url"], "title": result.get("title"),
                  "highlights": result.get("highlights", []), "text": result.get("text"), "publication_date": result.get("publishedDate"),
                  "discovered_at": now(), "query_id": response.get("requestId"), "rank": rank,
                  "source": "search", "purpose": "locations", "metadata_basis": "Raw Search result; Places structure only if present in raw response",
                  "raw": sanitize(result)}
        evidence.append(store.put("evidence", record))
    with store.transaction() as conn:
        current = store.get("accounts", account["id"], conn=conn)
        current["location_search_status"] = "completed"
        store.put("accounts", current, conn=conn)
    return evidence
