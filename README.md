# Account Watch

A local, evidence-first research workbench for finding regional fitness operators
with multiple operating US West Coast locations (California, Oregon, Washington) and documented expansion in the preceding
90 days. Five researched accounts is the target, not five guaranteed qualified leads.
No outreach, CRM writes, publication, or automatic paid setup actions.

## Setup

Requires Python 3.11+ (clean setup tested with Python 3.14), SQLite from Python's
standard library, and the exact dependencies in `requirements.txt`. `cloudflared`
is optional and only needed to receive real webhooks. Use these commands from
`account-watch/`:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pytest -q
```

Optional real-browser smoke check, with the private app already running (requires
`uv`; Playwright is a test tool, not an application dependency):

```sh
uv tool run --from playwright playwright install chromium
uv run --with playwright python tools/check_interface.py
```

The browser check blocks external requests and all writes. It checks JavaScript
errors, failed asset loads, controls, and desktop/mobile overflow without paid calls.
It also exercises all account/inspector tabs and recording-mode network isolation
using clearly labeled synthetic browser-only responses, never saved as live data.

The original `../.env` is never modified. Its case-sensitive `exa_key` is loaded
server-side through the explicit `--env-file` option. `jev_key` and `fin_data` are
not used. Missing credentials do not block the interface, recording view, or tests.
Never paste secrets into the UI, screenshots, source files, or recording exports.
`.env.example` contains blank optional configuration entries only.

Start the private application in one terminal:

```sh
.venv/bin/python tools/start.py ui --env-file ../.env
```

Open **http://localhost:8787**. The service binds only to loopback. The database is
`data/account-watch.sqlite3` by default. Back up your private data before replacing
or moving this directory. Do not commit the database or private secrets store.

Start the receiver in another terminal, using the same environment/database:

```sh
.venv/bin/python tools/start.py receiver --env-file ../.env
```

Receiver health: **http://localhost:8788/health**. The receiver exposes no UI,
credentials, research controls, API documentation, or application API routes.
The launcher disables access logging to keep opaque webhook tokens out of logs.

## Run research

1. Check the criteria bar: US West Coast (California, Oregon, Washington), multiple operating locations, and the displayed
   exact 90-day start/end dates. Click **Discover companies** to explicitly start
   at most two Search requests. Another discovery requires another explicit click.
2. Review the ranked shortlist, source links, and entity uncertainty. **Confirm
   domain** only after checking the company's official domain. A news publisher,
   directory, LinkedIn page, franchise brand, and franchisee are not interchangeable.
3. **Add to research** for up to five accounts, then **Research selected**. Each
   account gets at most one location Search and one initial Agent run. At most two
   Agent jobs run concurrently. Agent uses medium effort and requests Fiber company
   enrichment, not contacts. Partner usage may add cost.
4. Review all rows, including rejected and uncertain ones. **Locations found** is
   not an exhaustive branch count. Announced locations do not count as operating.
5. Use **Inspect request** for original sanitized requests/responses, grounding,
   lifecycle activity, usage, and reported cost. Missing costs say **Not reported**.
   Partner requested: Fiber does not establish field-level Fiber attribution.
6. For an actual ambiguity, choose a saved source and **Read more context**.
   At most two extra Contents reads per account are allowed. Review the old passage
   beside fuller context; **Confirm finding** or **Mark unresolved** with a note.
   Fetching context never automatically upgrades fit.

Known remote run IDs survive restarts. Refresh their status rather than starting a
new chargeable task. An ambiguous create timeout is not retried automatically.
Failed Fiber research is kept visible; web-only research requires a separate,
explicit action. Safe lookups have bounded retries; authentication and payment
errors do not trigger blind retry loops.

If monitor creation is uncertain, **Reconcile monitor** performs safe remote
lookups rather than another create. A recovered monitor whose one-time signing
secret was lost cannot safely receive updates; its `secret_unavailable` state
blocks checks/enabling, but still permits pausing it. An empty reconciliation
result alone cannot prove a timed-out creation will never finish, so it does not
automatically release the reservation or create a duplicate.

## Receive monitor updates

Only when ready to receive public webhook traffic, install Cloudflare's
`cloudflared`, leave the receiver running, then run:

```sh
cloudflared tunnel --url http://localhost:8788
```

**Never tunnel port 8787.** Do not add interactive email authentication to this
webhook tunnel: Exa is a non-interactive client. No tunnel configuration is changed
by the app. Quick Tunnels are development-only, have no uptime guarantee, and their
hostname changes on restart.

Set the resulting HTTPS base URL in the app's receiver configuration. Select a
verified account and explicitly click **Create monitor**. The app appends a private
opaque path token, subscribes to `monitor.run.completed`, and saves the one-time
signing secret in a restricted-permission local store. The initial monitor omits
`trigger`, so it is manual-only. **Check now** explicitly starts a run. Inspect run
history and completed output; the first check is not guaranteed to find anything.

When the tunnel URL changes, save the new base URL and update each existing
monitor's webhook URL with the monitor-panel control. Changing local configuration
alone does not update an existing remote monitor.

Signatures are checked against the exact raw body using HMAC-SHA256 over
`timestamp + '.' + raw_body`, with constant-time comparison. The default maximum
signature age is 300 seconds, configurable by signature tolerance. A retry with an
old signature outside that window is rejected even if its event was seen before;
Exa must send a fresh signed delivery or the user must reconcile the run. A fresh
valid retry is safely deduplicated by event and monitor/run identity. Do not disable
the age check to replay old captures: use sandbox replay instead.

Verified deliveries are persisted before acknowledgement and transactionally
applied. Missing grounding, unknown dates, entity mismatch, and out-of-region
findings remain reviewable rather than qualification evidence. Reviewed fit is
preserved and flagged **Needs recheck** when an update warrants review. An empty
completed run updates Last checked and shows **No new findings**, not proof that
the company has no expansion. Event, publication, and discovery dates remain
separate. A newly discovered page may describe an old event.

The laptop, receiver, and tunnel must stay running to receive updates. This is a
local demo, not hosted infrastructure. Monitor schedules are remote and can keep
running and accruing charges when the laptop is off.

## Run the recorded example

`examples/recorded-session/status.json` is explicitly **pending** until you perform
a genuine run. It is not an importable success recording. Tests use clearly labeled
synthetic examples only; they must never be presented as real customer evidence.

After real research, use **Save recording** or **Export JSON**. Exports remove
secrets and full copyrighted page bodies; restricted partner fields are excluded
conservatively. Check redistribution rights before sharing any excerpts. Private
raw evidence remains local. **Export CSV** produces one account per row with source
references; JSON retains the sanitized record structure and human corrections.

From a running private app, export using:

```sh
curl --fail http://localhost:8787/api/export/json -o data/session.json
curl --fail http://localhost:8787/api/export/csv -o data/accounts.csv
.venv/bin/python tools/import_recording.py data/session.json --env-file ../.env
```

For an isolated import:

```sh
.venv/bin/python tools/import_recording.py data/session.json --db data/import-test.sqlite3
```

Import validates and stores a read-only snapshot; it does not overwrite live
accounts, send Exa calls, or create/activate monitors. **Open recording** accepts
the JSON file, labels it **Recorded session** with its original date, and disables
live write controls and polling until you exit that view.

For a genuine previously verified delivery, **Replay delivery** applies it to a
separate sandbox starting from saved pre-update state, then applies it a second
time and displays row/signal counts and duplicate handling. It never sends an old
signature through the public endpoint and never updates the live account. The label
is **Replay of recorded delivery**; test-only data is labeled **Synthetic test**.

## Pause monitors

**Enable daily checks** explicitly patches an interval of `1d` and active status.
The minimum supported interval is one hour. Scheduled runs may be delayed up to
30 minutes. **Pause monitor** patches status to `paused`, stopping automatic runs
while preserving manual checks and history. Verify the returned status, and pause
unneeded schedules before ending the tutorial. The UI displays the returned next
run time rather than promising instant alerts.

Monitors discover new results/findings. They are not silent, fixed-page change
detection, and do not establish when a business event actually happened.

## What the evidence establishes

Qualification requires at least two distinct, supported operating locations across
California, Oregon, and Washington combined (not two per state), and supported
expansion in those states within the research window. The application enforces this
independently of Agent's generated fit explanation. Unknown dates, missing
field-level grounding, unverified identity, and unclear operating entities keep
fit provisional. Employee count is enrichment, not another qualification rule.

New discovery sessions use the West Coast scope. Saved Texas sessions, accounts,
research, recordings, and monitors keep their original scope; records missing a
geography are treated as legacy Texas data. No historical evidence is relabeled or
migrated. Discover and select the company in a new session to research it for the
West Coast: the same domain gets a separate regional account, and cross-region
merges are rejected. Existing remote Texas monitors are not rewritten. Workspace
summary totals include all saved geographies; account rows and exports identify
their scope. Restart the UI server and webhook receiver after upgrading, then
refresh the browser to load the updated JavaScript.

Places uses `/search`, not a separate Places endpoint or invented category. Generic
web results are not labeled confirmed Places metadata. Actual returned metadata
is retained for inspection. Addresses are deduplicated with operating entity;
five search results do not prove exhaustive location coverage.

Agent structured output is `output.structured`, with `output.text`,
`output.grounding`, lifecycle events, and reported cost kept separately. Monitors
instead return `output.results`, `output.content`, and `output.grounding`.
Attaching Fiber only proves it was requested. Lifecycle events are not a guaranteed
partner-tool trace; field attribution needs actual returned provenance.

Signal keys use account, source URL, type, event date, and location. This prevents
exact repeat application but is not perfect semantic deduplication. Review repeated
coverage and use **Merge duplicate** deliberately; original evidence and history
must remain inspectable.

Monitor timelines use a rolling 90-day relevance window ending at delivery time;
the original research window and reviewed fit remain unchanged. Imported replay
reports that prior verification is claimed by the recording, not cryptographically
revalidated. No import is trusted to activate a live monitor.

## Local Routes

| Method | URL | Purpose |
| --- | --- | --- |
| GET | `/api/config` | Safe configuration, never credentials |
| PATCH | `/api/config` | Receiver base URL and signature tolerance |
| GET | `/api/state` | Local progress and records; no Exa request |
| POST | `/api/discovery` | Explicit bounded discovery |
| POST | `/api/candidates/{id}/confirm` | Confirm official domain |
| POST | `/api/candidates/{id}/select` | Add account to research |
| POST | `/api/research` | Start selected research |
| POST | `/api/agent-runs/{id}/poll` | Resume known run lookup |
| GET | `/api/accounts/{id}` | Account with evidence/history |
| POST | `/api/accounts/{id}/context` | Read an associated evidence URL |
| POST | `/api/accounts/{id}/reviews` | Persist review decision/note |
| POST | `/api/accounts/{id}/merge` | Merge into target account |
| GET | `/api/artifacts/{id}` | Sanitized request inspector |
| POST | `/api/accounts/{id}/monitor` | Create manual monitor |
| POST | `/api/accounts/{id}/monitor/trigger` | Check now |
| POST | `/api/accounts/{id}/monitor/schedule` | Daily enable or pause |
| POST | `/api/accounts/{id}/monitor/reconcile` | Safe lookup of uncertain creation |
| PATCH | `/api/accounts/{id}/monitor/webhook` | Update receiver URL |
| GET | `/api/accounts/{id}/monitor/runs` | Inspect remote run history |
| GET | `/api/export/json` | Sanitized session recording |
| GET | `/api/export/csv` | One account per row |
| POST | `/api/recordings/save` | Save recording |
| POST | `/api/recordings/open` | Validate/open read-only recording |
| POST | `/api/replay/{delivery_id}` | Isolated duplicate-delivery demo |
| GET (8788) | `/health` | Receiver liveness |
| POST (8788) | `/webhooks/exa/{token}` | Signed Exa delivery only |

The replay route accepts optional `?recording_id={id}` to sandbox a delivery from
an imported recording instead of a verified live inbox record.

## Verification

The mocked suite covers domain/branch deduplication, merge aliases, selection and
concurrency caps, grounding and date rules, Agent failures/nulls, uncertain partner
provenance, Contents limits/failures, raw signatures and age/retry interactions,
wrong identities, concurrent duplicate deliveries, empty/failed results,
transaction recovery, human review preservation, pause/reconciliation, sandbox
isolation, export redaction, import commands, and receiver/UI route separation.

Dependency consistency is checked with `.venv/bin/python -m pip check`. The locked
Starlette version emits a test-client deprecation warning about its httpx transport;
it does not fail the tests. The app uses the requested `httpx` client.

Live Exa research, actual Places metadata, Fiber field provenance, real Contents
ambiguities, remote scheduling, and a genuine signed webhook still require your
explicit live actions. No successful recorded example is claimed before that.

## Resources

- [Search](https://exa.ai/docs/search/quickstart.md)
- [Places documentation](https://exa.ai/docs/search/data/sports-weather-places.md)
- [Places introduction](https://exa.ai/blog/places-search)
- [Agent](https://exa.ai/docs/agent/quickstart.md)
- [Agent events](https://exa.ai/docs/reference/agent-api/list-run-events)
- [Connect](https://exa.ai/docs/agent/connect/overview.md)
- [Fiber](https://exa.ai/docs/agent/connect/fiber.md)
- [Contents](https://exa.ai/docs/contents/quickstart.md)
- [Monitors](https://exa.ai/docs/monitors/quickstart.md)
- [Monitor update](https://exa.ai/docs/reference/monitors/update-a-monitor)
- [Monitor run](https://exa.ai/docs/reference/monitors/runs/get-a-run)
- [Cloudflare Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)
