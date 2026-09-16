# Entitlement Console — project conventions

POC proving governed data access on **Microsoft Fabric**: one entitlement table decides who sees
which rows and which columns of warehouse data. The success test everything serves:
*change one row in the entitlement table — two users instantly see different data.*

This folder is self-contained inside a shared public repo. Only touch `entitlement-console/`.

## Non-negotiables

- Never commit `.env`, secrets, tokens, or `node_modules/`. `git status` before every commit.
- Never rewrite history or force-push. Commits: prefix `entitlement-console:`, imperative mood, small.
  Repo-heal commits go direct to `main`; feature work on branches `ec/<topic>`.
- The repo is public: nothing sensitive in code, comments, commit messages, or seed data beyond the
  fictional rows in `server/routes/setup.js`.
- Every SQL statement executed against the warehouse stays visible to the operator — the
  `executed` log pattern (`columnRules.js`). Transparency is a design tenet, not a nicety.
- All warehouse DDL is idempotent (`IF NOT EXISTS` / `OBJECT_ID(...) IS NULL`). Re-running setup is always safe.
- Ask before anything destructive, anything that pushes, and before new scope.

## Architecture (authoritative — don't rediscover)

Node 18+, Express, `dotenv`. **No build step, no framework** — `public/` is vanilla JS served
statically by `server/index.js`, which mounts five routers under `/api`. Its error handler adds
friendly hints (`NOT_CONFIGURED` → point at `.env`; auth-pattern match → `az login` / SPN vars) and
forwards any `err.executed` SQL log.

- `server/db.js` — one `q(text, params)` contract over **two interchangeable backends** (tedious/`mssql`
  cannot reach Fabric — tediousjs/tedious#1563). `sqlcmd`: shells out to go-sqlcmd (default when the CLI is
  present; values validated+escaped into SQL, `@named`→literals). `odbc`: `odbc` npm + ODBC Driver 18
  (`@named`→positional `?`, bound). Auto-select, override with `EC_DB_DRIVER`. Auth is **Entra-only**
  (`az login` / ActiveDirectoryDefault, or the `AZURE_*` service principal). No SQL passwords in Fabric.
- `server/validate.js` — `assertEmail` (regex + length), `bracket()` (T-SQL identifier quoting that
  **rejects `]`** so the quote can't be escaped), `assertTableExists` / `assertColumnsExist`
  (whitelist every identifier against `sys.*` before it is interpolated into DDL).
  **Identifiers are whitelisted, data values are parameterized.** That split is the injection
  defense — preserve it in every new statement.
- Routes (`server/routes/`): `setup.js` (health, status, setup, preview), `meta.js` (tables, columns),
  `entitlements.js` (row rules + regions), `columnRules.js` (column grants), `changes.js` (evidence feed).
- `server/audit.js` — `logChange(action, detail)`: one parameterized INSERT into `gov.change_log`; actor is `USER_NAME()` server-side.

### API surface

`GET /api/health` · `GET /api/status` · `POST /api/setup` · `GET /api/preview?email&schema&table` ·
`GET /api/tables` · `GET /api/columns?schema&table` · `GET/POST/DELETE /api/entitlements` ·
`GET /api/regions` · `GET /api/column-rules?schema&table` ·
`POST /api/column-rules {user_email, schema, table, columns[]}` · `GET /api/changes?limit`

### What setup creates (idempotent)

Schemas `sales`/`gov`/`sec` · `sales.orders` seeded with 6 fictional rows (US/EU/APAC × 2) including
`customer_ssn` and `account_number` · `gov.entitlement(user_email, region)` ·
`gov.change_log(at, actor, action, detail)` (the audit trail every mutation writes to) · predicate
`sec.fn_rls_region` (`WITH SCHEMABINDING`, matches `USER_NAME()`, honors literal `'All'`) · security
policy `sec.orders_rls` (filter predicate on `sales.orders`, STATE = ON) · dynamic data mask
`partial(0,"XXXX-",4)` on `account_number` · finally entitles the connected identity to `All`
(the RLS filter applies to admins too while ON — without this the operator loses sight of the data).

### Column push (`POST /api/column-rules`)

`CREATE USER ... FROM EXTERNAL PROVIDER` if the principal is missing → `REVOKE` object-level and any
column-level SELECT → `GRANT` the exact allow-list (table-wide grant when the list equals all columns).
Every statement is appended to `executed` and returned to the UI, and the push ends with one
`gov.change_log` row (`column-rule.push`) whose detail carries that same SQL. The trail records console
actions only — direct SQL edits to `gov.entitlement` are not captured — and the operator identity can edit
the table: evidence for a POC, not tamper-proof.

### Preview is simulated

`GET /api/preview` is computed by the operator identity from grants + entitlement rows. It does
**not** simulate masking, and the response says so. Keep that honesty.

## UI conventions (design language is part of the deliverable)

Arial. Tokens: ink `#2B2F33`, gray `#5A6068`, hairline `#D9DBDE`, wash `#F1F2F4`, crimson `#A6242B`,
navy `#232F3E`, amber `#E08A3C`, blush `#F7E4E3` (line `#E4C4C2`).
**Blush background = "new / not yet enforced"** — a pending, unpushed change. Never repurpose blush.
Six hash-routed views: Overview & setup · Tables & columns · Row rules · Column rules · Preview as user · Evidence.
Navy status bar with amber label; toast for confirmations. Escape interpolated content with `esc()`;
render server `executed` logs verbatim in `.sqllog` blocks. No frameworks, bundlers, or CDN deps.

## Fabric ground rules (violating these silently breaks the demo)

1. Workspace **Admin/Member/Contributor bypass column security by design**; a workspace **Viewer**
   gets ReadData and bypasses CLS too. Demo users get **item share + SQL grants only, zero workspace
   roles**. The UI warns about this; keep the warning.
2. **Grants ≠ connect.** A user with perfect grants still can't connect until the warehouse is
   *Shared* with them in the Fabric portal (Share → email → leave every extra checkbox unticked).
   Once per user. Human step — the console can't do it.
3. RLS filter predicates apply **even to admins** while the policy is ON → setup self-entitles the
   operator to `All`.
4. `SCHEMABINDING` on the predicate forces `gov.entitlement` to live in the **same database** as the
   data. Don't propose moving it out.
5. The lakehouse SQL analytics endpoint is **read-only**; this console targets a **warehouse**.
   Don't "generalize" to lakehouses without redesign.
6. Masked columns show unmasked to `db_owner`-mapped identities (the operator sees real values;
   test users see `XXXX-…`). Expected, not a bug.

## Personas & expected results (verification matrix)

Fictional test identities in the operator's tenant; real emails supplied by the operator.
Regions: US / EU / APAC (2 rows each).

| Persona | Setup (human does the share; console does the grants) | Expected |
|---|---|---|
| **P1 Operator/Builder** | Workspace Admin (the only one); runs the console | All 6 rows, all columns, unmasked; setup + pushes succeed |
| **P2 Access admin** | Item share; grants on `gov.entitlement` **only** (SELECT/INSERT/UPDATE/DELETE) | Can add/remove row rules; `SELECT * FROM sales.orders` → permission denied (error 229). *Changes who sees data without seeing data.* |
| **P3 Analyst EU** | Item share; row rule EU; table-wide SELECT on `sales.orders` | Exactly 2 EU rows; all columns; `account_number` masked `XXXX-…` |
| **P4 Analyst APAC** | Item share; row rule APAC; column list = all columns **minus `customer_ssn`** | Exactly 2 APAC rows; `SELECT customer_ssn` → denied; `account_number` masked |

Headline check: flip P3's row rule EU → US in the Row rules tab → P3's very next query returns US
rows. No redeploy, no policy edit — one row changed in a table.

## Human-in-the-loop (ask, don't attempt)

`az login` as the operator · `.env` values (SQL connection string from Warehouse → Settings → SQL
endpoint, plus database name) · creating warehouse `wh_demo` if absent · sharing the warehouse to
P2–P4 · confirming test users hold no workspace roles · supplying test-user emails · approving every
push and any new scope.

## Verification

No-DB suite (always runnable): server boots without `.env`; `GET /api/health` → `configured:false`;
`GET /` serves the UI (catches the `Cannot GET /` regression); invalid email → 400; identifier with
`]` → 400. DB suite (needs `.env` + `az login`): `POST /api/setup` twice → both fully ok;
`/api/status` shows all five objects; preview for a P3-style email returns only entitled regions and
hides ungranted columns.

`npm run verify` (`scripts/verify.js`, no extra deps) runs the no-DB suite always, the DB suite when
`/api/status` reports connected, and the persona matrix when `VERIFY_P3_EMAIL` + `VERIFY_P4_EMAIL`
are set (P2 optional) — simulated via `/api/preview`; masking and real denials stay MANUAL rows with
the SQL to run. Unprovable checks are SKIP with a reason, never omitted (a boot failure ends the run);
exit 1 only on FAIL.
