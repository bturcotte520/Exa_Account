"""Read-only Chromium smoke check. Run with: uv run --with playwright python tools/check_interface.py"""
import argparse
import json
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8787")
    parser.add_argument("--screenshot", help="Optional desktop screenshot path (use only with non-sensitive data)")
    args = parser.parse_args()
    if urlsplit(args.url).hostname not in {"localhost", "127.0.0.1"}:
        parser.error("Only loopback application URLs are accepted")
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("response", lambda response: errors.append(f"HTTP {response.status}: {response.url}")
                if response.status >= 400 and not response.url.endswith("favicon.ico") else None)

        def read_only(route):
            request = route.request
            if request.method not in {"GET", "HEAD"} or urlsplit(request.url).hostname not in {"localhost", "127.0.0.1"}:
                route.abort()
            else:
                route.continue_()

        page.route("**/*", read_only)
        page.goto(args.url, wait_until="networkidle")
        page.get_by_role("button", name="Discover companies", exact=True).wait_for()
        assert "Account Watch" in page.title()
        assert page.locator("main details[open]").count() == 2
        assert page.locator("#criteria-panel").evaluate("element => element.open")
        assert page.locator("#account-detail").is_hidden()
        assert not errors, errors
        desktop_overflow = page.evaluate("document.documentElement.scrollWidth > innerWidth")
        assert not desktop_overflow, "Desktop viewport overflows"
        if args.screenshot:
            page.screenshot(path=args.screenshot, full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.get_by_role("button", name="Discover companies", exact=True).is_visible()
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth"), "Mobile viewport overflows"
        for disclosure in page.locator("main > details, .ledger").all():
            disclosure.evaluate("element => element.open = true")
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth"), "Expanded mobile viewport overflows"
        assert not errors, errors
        page.close()

        # Synthetic browser-only records never reach the live database or Exa.
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.on("pageerror", lambda error: errors.append(str(error)))
        account = {"id": "synthetic-account", "name": "Synthetic test operator", "domain": "example.com",
                   "domain_verified": True, "session_id": "synthetic-session", "window_start": "2026-08-08",
                    "window_end": "2026-11-06", "geography": "US West Coast (California, Oregon, Washington)", "fit": "needs_review", "fit_explanation": "# Synthetic assessment\\n\\nSynthetic test only. <img src=x onerror=alert(1)>\\n\\n## Findings\\n- **Operating** locations need review\\n- [Unsafe](javascript:alert) and [Source](https://example.com)\\n\\nApplication checks: entity needs review.",
                    "locations": [{"name": "Test site", "operating_status": "operating", "supported": True,
                                    "state": "CA", "address": "Synthetic address\nTest City, CA",
                                    "provenance": {"address": [{"url": "https://example.com/site", "title": "Site source"}],
                                                   "name": [{"url": "https://example.com/site", "title": "Site source"}]}}],
                    "criteria_assessment": {"accounts": {"status": "unknown", "explanation": "## Account type\n\nNeeds review.\n\n1. Check entity\n2. Verify source"}},
                    "unresolved_questions": ["Check the entity", "Confirm address"],
                    "expansion_signals": [{"type": "opening", "description": "## Synthetic signal\n\n- Announced\n- Needs review", "supported": True}],
                   "employee_count": None, "partner_requested": True}
        evidence = {"id": "synthetic-evidence", "account_id": account["id"], "title": "Test source",
                     "url": "https://example.com", "source": "search", "purpose": "locations",
                     "highlights": ["## Synthetic evidence\n\nFirst passage.\n\n- Branch one\n- Branch two", {"excerpt": "Structured passage", "status": "Unconfirmed"}],
                     "text": "# Returned page\n\nA readable page paragraph.\n\n1. Address review\n2. Status review\n\nKeep `C:\\new\\notes`. <script>window.injected = true</script>"}
        artifact = {"id": "synthetic-artifact", "account_id": account["id"], "operation": "agent_poll",
                    "request": {"method": "GET", "path": "/agent/runs/synthetic"},
                    "response": {"output": {"structured": {}, "grounding": []}}, "cost_dollars": {"total": 0.1}}
        state = {"accounts": [account], "sessions": [{"id": "synthetic-session", "created_at": "2026-11-06T00:00:00Z",
                 "window_start": "2026-08-08", "window_end": "2026-11-06", "geography": account["geography"], "status": "completed"}],
                 "artifacts": [artifact], "evidence": [evidence], "candidates": [{"id": "synthetic-candidate", "name": "Candidate test",
                  "domain": "example.org", "session_id": "synthetic-session", "domain_verified": False,
                  "evidence_ids": [evidence["id"]]}]}
        recording = {"format": "account-watch", "version": 1, "recorded_at": "2026-11-06T00:00:00Z",
                     "synthetic": True, "label": "Synthetic test", "records": state}
        opened = False
        calls_after_open = []
        writes = []
        reject_research = True
        config = {"api_key_configured": False, "receiver_url": "", "signature_tolerance": 300}

        def synthetic_route(route):
            nonlocal opened
            request = route.request
            path = urlsplit(request.url).path
            if not path.startswith("/api/"):
                return read_only(route)
            if opened:
                calls_after_open.append(path)
            if request.method not in {"GET", "HEAD"}:
                writes.append((path, request.post_data_json))
            if path == "/api/state":
                value = state
            elif path == "/api/config":
                if request.method == "PATCH":
                    config.update(request.post_data_json)
                value = config
            elif path == "/api/accounts/synthetic-account":
                value = {"account": account, "artifacts": [artifact], "evidence": [evidence], "monitors": state.get("monitors", [])}
            elif path == "/api/discovery":
                value = {"id": "synthetic-discovery", **request.post_data_json}
            elif path == "/api/discovery/preview":
                value = {"requests": [{"query": f"Synthetic {kind}: {request.post_data_json['geography']}", "type": "auto"} for kind in ("company discovery", "expansion evidence")]}
            elif path == "/api/research":
                if reject_research:
                    return route.fulfill(status=422, content_type="application/json", body=json.dumps({"detail": [{"loc": ["body", "account_ids"], "msg": "Synthetic validation failure"}]}))
                value = {"runs": [{"id": "synthetic-run", "account_id": account["id"], "status": "queued_local", "partner_requested": True}]}
                state["agent_runs"] = value["runs"]
            elif path == "/api/accounts/synthetic-account/monitor/preview":
                value = {"request": {"search": {"query": "Synthetic monitor query for existing company"}, "metadata": {"account_id": account["id"]}, "webhook": {"url": (config["receiver_url"] or "https://receiver.example.com") + "/webhooks/exa/assigned-on-create", "events": ["monitor.run.completed"]}, "outputSchema": {"properties": {"signals": {"items": {"properties": {"eventDate": {}, "description": {}}}}}}}, "receiver_configured": bool(config["receiver_url"]), "note": "Preview only. No remote monitor created."}
            elif path == "/api/accounts/synthetic-account/context":
                value = {**evidence, "id": "synthetic-context", "source": "contents", "previous_passage": evidence["highlights"], "text": "Synthetic fuller context", "status": "completed"}
                state["evidence"].append(value)
            elif path == "/api/accounts/synthetic-account/reviews":
                value = {"id": "synthetic-review", "account_id": account["id"], **request.post_data_json}
                state["reviews"] = [value]
            elif path == "/api/candidates/synthetic-candidate/select":
                state["candidates"][0]["selected"] = True
                value = {"candidate": state["candidates"][0], "account": account}
            elif path == "/api/accounts/synthetic-account/monitor":
                value = {"id": account["id"], "account_id": account["id"], "remote_id": "test-monitor", "status": "active", "secret_available": True}
                state["monitors"] = [value]
            elif path == "/api/accounts/synthetic-account/monitor/trigger":
                value = {"id": "test-monitor-run", "status": "queued"}
            elif path == "/api/accounts/synthetic-account/monitor/schedule":
                state["monitors"][0]["status"] = "active" if request.post_data_json["enabled"] else "paused"
                value = state["monitors"][0]
            elif path == "/api/artifacts/synthetic-artifact":
                value = artifact
            elif path == "/api/recordings/open" and request.method == "POST":
                opened = True
                value = {"id": "synthetic-recording", "read_only": True, "recording": recording}
            else:
                raise AssertionError(f"Unexpected UI request: {request.method} {path}")
            route.fulfill(status=200, content_type="application/json", body=json.dumps(value))

        page.route("**/*", synthetic_route)
        page.goto(args.url, wait_until="networkidle")
        assert page.locator("main details[open]").count() == 2
        assert page.get_by_role("navigation", name="Research workflow").is_visible()
        assert page.locator("#research-geography").text_content() == account["geography"]
        page.locator('[data-stage="research"]').click()
        assert page.locator("#account-detail").is_visible()
        page.locator('[data-stage="review"]').click()
        expect(page.get_by_role("tab", name="Evidence", exact=True)).to_have_attribute("aria-selected", "true")
        page.locator('[data-stage="criteria"]').click()
        page.get_by_label("Geography", exact=True).fill("Japan")
        page.get_by_label("Accounts", exact=True).fill("Independent hotels")
        page.get_by_label("Qualifications", exact=True).fill("Newly renovated")
        page.get_by_label("Days", exact=True).fill("30")
        page.locator("#preview-search").click()
        page.locator("#search-preview").wait_for(state="visible")
        assert "Company discovery" in page.locator("#search-preview").inner_text()
        assert "Expansion evidence" in page.locator("#search-preview").inner_text()
        writes.clear()
        page.locator("#discover").click()
        assert "Japan" in page.locator("#confirm-description").inner_text()
        page.locator("#confirm-cancel").click()
        assert not writes
        page.locator("#discover").click()
        page.locator("#confirm-proceed").click()
        page.locator("#candidates-dialog").wait_for(state="visible")
        assert writes[-1] == ("/api/discovery", {"geography": "Japan", "accounts": "Independent hotels", "qualifications": "Newly renovated", "window_days": 30})
        page.keyboard.press("Escape")
        page.get_by_label("Research window", exact=True).select_option("dates")
        page.get_by_label("Start date", exact=True).fill("2026-02-01")
        page.get_by_label("End date", exact=True).fill("2026-01-01")
        page.locator("#discover").click()
        assert page.locator("#confirm-dialog").is_hidden()
        assert "ordered" in page.locator("#error-message").inner_text()
        page.locator("#dismiss-error").click()
        page.get_by_label("End date", exact=True).fill("2026-03-01")
        page.locator("#discover").click()
        page.locator("#confirm-proceed").click()
        page.locator("#candidates-dialog").wait_for(state="visible")
        assert writes[-1][1]["window_start"] == "2026-02-01"
        assert writes[-1][1]["window_end"] == "2026-03-01"
        page.keyboard.press("Escape")
        page.locator("#candidates-button").click()
        candidate = page.locator(".candidate-record")
        assert candidate.evaluate("element => element.open")
        assert candidate.locator(".research-content h5").inner_text() == "Synthetic evidence"
        assert candidate.locator(".research-fields dt").all_text_contents() == ["excerpt", "status"]
        assert page.get_by_role("button", name="Confirm domain", exact=True).count() == 0
        page.wait_for_timeout(4500)
        assert candidate.evaluate("element => element.open"), "Polling collapsed an expanded candidate"
        page.get_by_role("button", name="Add to research", exact=True).click()
        page.locator("#review-selection").click()
        assert page.locator("#research").is_enabled()
        assert page.locator('input[type="checkbox"]').is_checked()
        assert page.locator("#account-detail").is_visible(), "Add must open account section access"
        assert any(path.endswith("/select") for path, _ in writes)
        assert not any(path.endswith("/confirm") for path, _ in writes)
        page.locator("#research").click()
        page.locator("#confirm-dialog").wait_for(state="visible")
        assert "Fiber requested" in page.locator("#confirm-description").inner_text()
        assert page.locator("#busy-status").is_visible()
        page.locator("#confirm-cancel").click()
        assert not any(path == "/api/research" for path, _ in writes)
        page.locator("#research").click()
        page.locator("#confirm-proceed").click()
        page.wait_for_function("document.getElementById('error-message').textContent.includes('Synthetic validation failure')")
        assert "body.account_ids" in page.locator("#error-message").inner_text()
        assert page.locator("#research").is_enabled()
        reject_research = False
        page.locator("#research").click()
        page.locator("#confirm-proceed").click()
        page.locator("#research-progress").wait_for(state="visible")
        assert "queued local" in page.locator("#research-progress").inner_text()
        assert page.locator("#account-detail").is_visible()
        assert page.locator("#research").is_disabled(), "Accepted runs must leave the next batch selection clear"
        previous_count = sum(path == "/api/research" for path, _ in writes)
        page.locator('input[type="checkbox"]').check()
        page.locator("#research").click()
        page.wait_for_function("document.getElementById('error-message').textContent.includes('already has saved research')")
        assert page.locator("#confirm-dialog").is_hidden()
        assert sum(path == "/api/research" for path, _ in writes) == previous_count
        assert "1 supported operating" in page.locator("#account-rows").inner_text()
        page.locator(".company-button").click()
        assert page.locator("#account-detail").is_visible()
        assert page.locator(".fit-explanation p").count() >= 2
        assert page.locator(".fit-explanation h4").inner_text() == "Synthetic assessment"
        assert page.locator(".fit-explanation li").count() == 2
        assert page.locator(".fit-explanation strong").inner_text() == "Operating"
        assert page.locator('.fit-explanation a[href^="javascript:"]').count() == 0
        assert page.locator(".assessment-block ol li").count() == 2
        assert page.locator("#account-panel img").count() == 0
        assert "Places-specific attribution is unconfirmed" in page.locator("#account-panel").inner_text()
        assert "Provider use and field attribution are unconfirmed" in page.locator("#account-panel").inner_text()
        assert page.locator("#account-panel details[open]").count() == 0
        for name in ("Overview", "Locations", "Evidence", "Partner data", "Updates", "Monitor"):
            page.get_by_role("tab", name=name, exact=True).click()
            assert page.locator("#account-panel details[open]").count() == 0
            for summary in page.locator("#account-panel summary").all():
                summary.click()
            page.set_viewport_size({"width": 390, "height": 844})
            assert not page.evaluate("document.documentElement.scrollWidth > innerWidth"), f"{name} mobile overflow"
            page.set_viewport_size({"width": 1920, "height": 1080})
        page.get_by_role("tab", name="Locations", exact=True).click()
        assert "Physical locations" in page.locator("#account-panel").inner_text()
        location = page.locator(".location-record")
        assert location.locator("dt").all_text_contents() == ["Label", "Address", "Operating status", "State", "Operating entity", "Evidence status"]
        assert "Synthetic address\nTest City, CA" in location.inner_text()
        assert location.locator(".source-meta a").count() == 1, "Repeated field citations should share one location source link"
        assert page.locator("#account-panel .record").first.get_attribute("class") == "record location-record"
        for width in (320, 390, 768, 1920):
            page.set_viewport_size({"width": width, "height": 844})
            assert not page.evaluate("document.documentElement.scrollWidth > innerWidth"), f"Locations overflow at {width}px"
            assert location.evaluate("element => element.scrollWidth <= element.clientWidth"), f"Location fields overflow at {width}px"
        page.get_by_role("tab", name="Evidence", exact=True).click()
        assert page.locator(".evidence-passages blockquote li").count() == 2
        assert page.locator(".evidence-passages .research-fields dt").all_text_contents() == ["excerpt", "status"]
        assert page.locator(".source-meta .metadata dt").all_text_contents() == ["Published", "Discovered"]
        assert page.locator(".page-text").evaluate("element => !element.open")
        page.locator(".page-text summary").click()
        assert page.locator(".page-text ol li").count() == 2
        assert page.locator(".page-text code").inner_text() == "C:\\new\\notes"
        assert page.locator(".page-text script").count() == 0
        assert page.evaluate("window.injected === undefined")
        for width in (320, 390, 768, 1920):
            page.set_viewport_size({"width": width, "height": 844})
            assert not page.evaluate("document.documentElement.scrollWidth > innerWidth"), f"Rich evidence overflows at {width}px"
            assert page.locator(".evidence-passages").evaluate("element => element.scrollWidth <= element.clientWidth"), f"Evidence content overflows at {width}px"
        page.locator(".page-text summary").focus()
        page.keyboard.press("Enter")
        assert page.locator(".page-text").evaluate("element => !element.open"), "Page context must be keyboard accessible"
        assert page.get_by_role("button", name="Read more context", exact=True).is_visible()
        page.get_by_role("button", name="Read more context", exact=True).click()
        page.locator("#confirm-proceed").click()
        page.wait_for_function("document.getElementById('account-panel').textContent.includes('Synthetic fuller context')")
        page.get_by_label("Review note", exact=True).first.fill("Synthetic source checked")
        page.get_by_role("button", name="Mark unresolved", exact=True).first.click()
        page.wait_for_function("document.getElementById('notice').textContent.includes('Review decision saved')")
        assert any(path.endswith("/context") and "query" not in body for path, body in writes), "Blank query must use server default, not fail validation"
        assert any(path.endswith("/reviews") and body["note"] == "Synthetic source checked" for path, body in writes)
        page.get_by_role("tab", name="Monitor", exact=True).click()
        page.wait_for_function("document.querySelector('.monitor-preview').textContent.includes('Attach to existing account')")
        assert "eventDate" in page.locator(".monitor-preview").inner_text()
        assert "assigned-on-create" in page.locator(".monitor-preview").inner_text()
        assert page.get_by_role("button", name="Create monitor", exact=True).is_disabled()
        page.get_by_role("button", name="Configure receiver", exact=True).click()
        page.locator("#receiver-url").fill("https://receiver.example.com")
        page.get_by_role("button", name="Save", exact=True).click()
        page.get_by_role("button", name="Create monitor", exact=True).click()
        page.locator("#confirm-proceed").click()
        page.get_by_role("button", name="Check now", exact=True).wait_for()
        page.get_by_role("button", name="Check now", exact=True).click()
        page.locator("#confirm-cancel").click()
        assert not any(path.endswith("/trigger") for path, _ in writes)
        page.get_by_role("button", name="Check now", exact=True).click()
        page.locator("#confirm-proceed").click()
        page.get_by_role("button", name="Enable daily checks", exact=True).click()
        page.locator("#confirm-proceed").click()
        page.get_by_role("button", name="Pause monitor", exact=True).click()
        page.wait_for_function("document.getElementById('account-panel').textContent.includes('paused')")
        assert any(path.endswith("/trigger") for path, _ in writes)
        assert any(path.endswith("/schedule") and body == {"enabled": True} for path, body in writes)
        assert any(path.endswith("/schedule") and body == {"enabled": False} for path, body in writes)
        page.get_by_text("Workspace tools", exact=True).click()
        page.locator("#inspect-button").click()
        for name in ("Request", "Response", "Grounding", "Activity"):
            page.get_by_role("tab", name=name, exact=True).click()
        page.keyboard.press("Escape")
        page.locator("#recording-file").set_input_files({"name": "synthetic-test.json", "mimeType": "application/json",
                                                        "buffer": json.dumps(recording).encode()})
        page.locator("#recording-banner").wait_for(state="visible")
        assert page.locator("#recording-label").inner_text() == "Synthetic test"
        assert page.get_by_role("button", name="Discover companies", exact=True).is_disabled()
        page.locator(".company-button").click()
        page.get_by_role("tab", name="Monitor", exact=True).click()
        page.wait_for_timeout(4500)
        assert calls_after_open == [], calls_after_open
        account.pop("geography")
        account["locations"][0]["state"] = "TX"
        state["sessions"][0].pop("geography")
        page.locator("#recording-file").set_input_files({"name": "legacy-synthetic-test.json", "mimeType": "application/json",
                                                        "buffer": json.dumps(recording).encode()})
        page.wait_for_function("document.getElementById('research-geography').textContent === 'Texas'")
        assert "1 supported operating" in page.locator("#account-rows").inner_text()
        assert not errors, errors
        browser.close()
    print("Desktop/mobile, staged navigation, query previews, editable criteria/dates, Add -> selected -> Research paid confirmation/cancel, 422 errors, queued progress, duplicate-run guard, safe Markdown/escaped-newline formatting, structured location fields and source deduplication, evidence passages/page context separated from metadata, keyboard disclosure, 320/390/768/1920px layout, Places/Agent/Connect/Contents/review/Monitor previews and mocked controls, inspector and disconnected recordings passed. Synthetic API responses only; live writes and external requests blocked.")


if __name__ == "__main__":
    main()
