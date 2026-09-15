# Revenue dashboard debugging — findings, fixes, and Loom script

Stack: `docker-compose up --build` → frontend http://localhost:3000, API http://localhost:8000 (docs at /docs).
Logins: `sunset@propertyflow.com / client_a_2024` (tenant-a, Sunset Properties),
`ocean@propertyflow.com / client_b_2024` (tenant-b, Ocean Rentals).
Reset everything (Redis + Postgres have no volumes): `docker-compose down && docker-compose up --build`.
Backend is volume-mounted with `--reload`; the frontend was not changed.

Repro helper: `bash scripts/revenue_check.sh` — logs in as both tenants, calls
`GET /api/v1/dashboard/summary?property_id=…` for all five properties as each tenant, then runs the
cross-tenant leak sequence and lists the Redis keys.

## Commits (one bug each, oldest first)

| # | commit | what |
|---|---|---|
| 1 | `9015244` | pool never connected → every number was mock data |
| 2 | `59f9370` | cache key had no tenant → cross-tenant leak |
| 3 | `0a6a7b9` | sub-cent sums passed through as float → cents off |
| 4 | `e477711` | "March" defined in the property's timezone (optional `month`/`year` params) |
| 5 | `5262ded` | mock fallback → HTTP 503; missing tenant → HTTP 401 |

## Before-numbers (unfixed code, commit `d1835ec`)

Every call, for both tenants, for every property, returned the same hardcoded numbers:

| property | tenant-a (Sunset) saw | tenant-b (Ocean) saw |
|---|---|---|
| prop-001 | 1000.00 / 3 bookings | 1000.00 / 3 bookings |
| prop-002 | 4975.50 / 4 | 4975.50 / 4 |
| prop-003 | 6100.50 / 2 | 6100.50 / 2 |
| prop-004 | 1776.50 / 4 | 1776.50 / 4 |
| prop-005 | 3256.00 / 3 | 3256.00 / 3 |

Backend log on every request: `Database error for prop-001 (tenant: tenant-a): Database pool not available`,
and at startup `Database pool initialization failed: 'Settings' object has no attribute 'supabase_db_user'`.
The numbers above are the mock dict in `reservations.py`, not the database. Note the mock for prop-001
(1000.00 / 3) is exactly what a UTC "March" would give, so Sunset's complaint reproduces either way.

Ground truth in Postgres:

| tenant | property | all-time | March (UTC) | March (property local tz) |
|---|---|---|---|---|
| tenant-a | prop-001 (Europe/Paris) | 2250.000 / 4 | **1000.000 / 3** | **2250.000 / 4** |
| tenant-a | prop-002 (Europe/Paris) | 4975.500 / 4 | same | same |
| tenant-a | prop-003 (Europe/Paris) | 6100.500 / 2 | same | same |
| tenant-b | prop-001 (America/New_York) | no reservations | 0 | 0 |
| tenant-b | prop-004 (America/New_York) | 1776.500 / 4 | same | same |
| tenant-b | prop-005 (America/New_York) | 3256.000 / 3 | same | same |

## After-numbers (HEAD)

| property | tenant-a (Sunset) | tenant-b (Ocean) |
|---|---|---|
| prop-001 | 2250.00 / 4 | 0.00 / 0 (Mountain Lodge has no bookings) |
| prop-002 | 4975.50 / 4 | 0.00 / 0 (not theirs) |
| prop-003 | 6100.50 / 2 | 0.00 / 0 (not theirs) |
| prop-004 | 0.00 / 0 (not theirs) | 1776.50 / 4 |
| prop-005 | 0.00 / 0 (not theirs) | 3256.00 / 3 |

Sunset prop-001 with `month=3&year=2024` → 2250.00 / 4; with `month=2&year=2024` → 0.00 / 0.
UI (http://localhost:3000, unchanged frontend image) verified: Sunset → Beach House Alpha shows
"USD 2,250.00 / 4 bookings"; Ocean → prop-001 shows "USD 0.00 / 0 bookings".

## Bugs

### 1. The dashboard never read the database (root of Symptom 1, and what hid Symptom 2)
- **Symptom:** identical totals for every tenant; Sunset's March total (1000.00 / 3) ≠ their records (2250.00 / 4).
- **Root cause:** `backend/app/core/database_pool.py:18` built the URL from `settings.supabase_db_user` etc.,
  which don't exist on `Settings`. `initialize()` raised, `session_factory` stayed `None`, and
  `reservations.py:88-109` caught the error and returned a hardcoded mock dict with a 200.
  Two more faults sat behind it: `poolclass=QueuePool` is rejected by async engines, and
  `get_session()` was `async def`, so `async with db_pool.get_session()` got a coroutine.
  The service also built a fresh 20-connection engine per request.
- **Fix (commit 1):** engine from `settings.database_url` with the `asyncpg` driver, `AsyncAdaptedQueuePool`,
  synchronous `get_session()`, reuse of the module-level `db_pool`.
- **Before/after:** table above. Both tenants now get their own rows; unowned properties return 0.

### 2. Cross-tenant cache leak (Symptom 2, privacy)
- **Symptom:** Ocean sometimes saw another company's revenue after refresh.
- **Root cause:** `backend/app/services/cache.py:13` keyed Redis as `revenue:{property_id}`. `prop-001` exists in
  both tenants (Beach House Alpha / Mountain Lodge Beta). Whoever populated the key first served their numbers
  to the other tenant for the 5-minute TTL. "Sometimes" = whether the other tenant's entry was still warm.
- **Fix (commit 2):** key = `revenue:{tenant_id}:{property_id}` (commit 4 adds `:{period}`).
- **Before/after:** flush → A views prop-001 (2250.00 / 4) → B views prop-001: before **2250.00 / 4** (Sunset's),
  after **0.00 / 0**. Redis: before one key `revenue:prop-001`; after `revenue:tenant-a:prop-001:all` and
  `revenue:tenant-b:prop-001:all`.

### 3. Totals off by cents (Symptom 3)
- **Symptom:** finance saw totals a few cents off.
- **Root cause:** `reservations.total_amount` is `NUMERIC(10,3)` so `SUM()` carries sub-cent precision.
  `reservations.py:68` passed it through, `dashboard.py:18` converted to `float`, and
  `frontend/src/components/RevenueSummary.tsx:64` did `Math.round(x*100)/100`. Rounding happened last, in
  binary float, at the display layer, with no defined rounding mode. Example: a 100.005 booking → API
  `100.005` → UI shows **100.00** (JS: `100.005*100 = 10000.4999…`). Correct financial rounding is 100.01.
- **Fix (commit 3):** `Decimal(...).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)` once, in the service.
  Response shape unchanged; the frontend's rounding is now a no-op and "Precision Mismatch" no longer lights up.
- **Before/after:** 100.005 booking → before `100.005`, after `100.01`. Seed totals unchanged (they happen to
  sum to whole cents: 333.333 + 333.333 + 333.334 + 1250.000 = 2250.000).

### 4. "March" was undefined, and the only monthly code was a dead UTC stub (Symptom 1, timezone half)
- **Symptom:** Sunset's Paris property has a booking checking in `2024-02-29 23:30 UTC` = `2024-03-01 00:30`
  Europe/Paris. Any monthly cut in UTC drops it (1000.00 / 3 vs 2250.00 / 4).
- **Root cause:** `reservations.py:5-32` `calculate_monthly_revenue` returned `Decimal('0')`, was never called,
  and built naive UTC bounds against a `timestamptz` column. The summary endpoint had no month notion at all,
  although `properties.timezone` exists.
- **Fix (commit 4):** optional `month` + `year` query params on `/dashboard/summary` (both or neither; default
  = all-time so the UI is unchanged). SQL joins `properties` and filters on
  `(check_in_date AT TIME ZONE p.timezone)`, i.e. the calendar month as the property experiences it.
  Cache key gains the period. Dead stub removed.
- **Before/after:** Sunset prop-001 `month=3&year=2024` → 2250.00 / 4; `month=2&year=2024` → 0.00 / 0.
  A UTC cut of the same data gives 1000.00 / 3 (SQL table above).

### 5. Silent fake data on DB failure, and a made-up default tenant
- **Symptom:** DB unreachable → 200 with plausible property-specific numbers (this is how bug 1 went unnoticed).
- **Root cause:** `reservations.py:88-109` catch-all returned the mock dict; `dashboard.py:14` substituted
  `"default_tenant"` when the user had no tenant and queried/cached under it.
- **Fix (commit 5):** log the real error, raise 503 `Revenue data temporarily unavailable`; raise 401 when the
  user has no `tenant_id`.
- **Before/after:** `docker-compose stop db` → before 200 with 1000.00 / 3; after **503**. `docker-compose start db`
  → 200 again without restarting the backend.

## Found but not fixed (off-path or not worth the diff)
- `reservations.py` hardcodes `"currency": "USD"` although `reservations.currency` exists. All seed rows are USD
  so no visible effect. Fix would be `GROUP BY currency` and deciding what to do with mixed currencies.
- `app/core/tenant_resolver.py:92` falls back to `"tenant-a"` for any unknown email, and `auth.py` accepts any
  HS256 token signed with `SECRET_KEY` (`debug_challenge_secret` in docker-compose). Auth machinery, out of scope;
  the 401 guard in commit 5 only fires if the resolver returns nothing.
- `RevenueSummary.tsx` sends an `X-Simulated-Tenant: candidate` header the backend ignores, and shows a fake
  "+12%" trend badge. Cosmetic.
- `RevenueSummary.tsx:64` still rounds client-side; harmless now that the API returns cents, left to avoid a
  frontend rebuild.
- The pool is created lazily on first request and never disposed on shutdown (`DatabasePool.close` exists but
  nothing calls it). Fine for dev.
- No pytest added: the repo has no test setup for this path and the curl script covers every fix in <30 s.

## Loom demo script (5–10 min)

Prep before recording: `git log --oneline -6` open in one terminal, `scripts/revenue_check.sh` handy, browser on
http://localhost:3000. Optional: to show the broken state live, run the "unfixed" block first.

**0. (Optional, 1 min) Show the broken state.**
```
git checkout d1835ec -- backend   # revert backend only; --reload picks it up in ~2 s
docker-compose exec -T redis redis-cli FLUSHALL
bash scripts/revenue_check.sh
```
Expect: both tenants see 1000.00 / 3 for prop-001 and the same five numbers as each other;
backend log says `Database pool not available`; one Redis key `revenue:prop-001`.
Then restore: `git checkout HEAD -- backend` (tree is clean again; `git status` to confirm).

**1. (1 min) Clean start.**
```
docker-compose down && docker-compose up --build -d
```
Login in the UI as Sunset → dashboard, Beach House Alpha (prop-001): **USD 2,250.00, 4 bookings**.
Switch to City Apartment (prop-002): 4,975.50 / 4. Country Villa (prop-003): 6,100.50 / 2.

**2. (1 min) Bug 1 — pool never connected.** Show `database_pool.py` diff (commit 1): `settings.supabase_db_*`
never existed → mock data. Point at the mock dict that used to be in `reservations.py`.

**3. (2 min) Bug 2 — leak.** Run `bash scripts/revenue_check.sh`. Read out the leak section:
A prop-001 = 2250.00 / 4, B prop-001 = **0.00 / 0**, Redis has two tenant-scoped keys.
Show `cache.py` diff (commit 2). Say: before the fix, B's line read 2250.00 / 4 (Sunset's money) and
Redis held one key `revenue:prop-001`. Log out, log in as Ocean in the UI: prop-001 shows 0.00 / 0,
Lakeside Cottage (prop-004) 1,776.50 / 4, Urban Loft (prop-005) 3,256.00 / 3.

**4. (1.5 min) Bug 4 — what "March" means.** With a Sunset token:
```
TA=$(curl -s -X POST localhost:8000/api/v1/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"sunset@propertyflow.com","password":"client_a_2024"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -s "localhost:8000/api/v1/dashboard/summary?property_id=prop-001&month=2&year=2024" -H "Authorization: Bearer $TA"; echo
curl -s "localhost:8000/api/v1/dashboard/summary?property_id=prop-001&month=3&year=2024" -H "Authorization: Bearer $TA"; echo
```
Expect Feb: 0.00 / 0, March: 2250.00 / 4. Show the seed row `res-tz-1` (`2024-02-29 23:30:00+00`, Paris)
and the `AT TIME ZONE p.timezone` line in `reservations.py`. Mention a UTC cut would say 1000.00 / 3.

**5. (1.5 min) Bug 3 — cents.** Insert a sub-cent booking for Ocean's empty prop-001, flush cache, query:
```
docker-compose exec -T db psql -U postgres -d propertyflow -c "INSERT INTO reservations VALUES ('res-cent','prop-001','tenant-b','2024-03-10 12:00+00','2024-03-12 12:00+00',100.005);"
docker-compose exec -T redis redis-cli FLUSHALL
TB=$(curl -s -X POST localhost:8000/api/v1/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"ocean@propertyflow.com","password":"client_b_2024"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -s "localhost:8000/api/v1/dashboard/summary?property_id=prop-001" -H "Authorization: Bearer $TB"; echo
```
Expect **100.01**. Say: before, the API returned 100.005 and the UI rounded it to 100.00 in JS. Show the
`quantize(..., ROUND_HALF_UP)` line (commit 3). Clean up:
`docker-compose exec -T db psql -U postgres -d propertyflow -c "DELETE FROM reservations WHERE id='res-cent';"`

**6. (1 min) Bug 5 — no more fake numbers.**
```
docker-compose stop db; docker-compose exec -T redis redis-cli FLUSHALL
curl -s -w ' [%{http_code}]' "localhost:8000/api/v1/dashboard/summary?property_id=prop-001" -H "Authorization: Bearer $TA"; echo
docker-compose start db; sleep 4
curl -s -w ' [%{http_code}]' "localhost:8000/api/v1/dashboard/summary?property_id=prop-001" -H "Authorization: Bearer $TA"; echo
```
Expect 503 then 200 / 2250.00. Show the commit-5 diff: "a 200 with invented revenue is the worst failure mode
for a finance dashboard".

**7. (30 s) Wrap.** `git log --oneline -6`; five commits, one root cause each; found-but-not-fixed list above.

## Before submitting
- [ ] `git push origin main` (pushes were blocked from the agent session; verify with `git status -sb`).
- [ ] Record the Loom, add the link here: **Loom:** _(link)_
- [ ] Commit this file with the link and push again.
