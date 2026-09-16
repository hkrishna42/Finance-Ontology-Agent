# Entitlement Console — POC

A small web console that connects to a **Microsoft Fabric warehouse SQL endpoint** and lets an
administrator control **row-level and column-level access** through an **entitlement layer**:

- see the list of tables and columns in the warehouse
- add / remove **row rules** (who sees which regions) — applied **instantly**, because the
  Fabric security policy reads the entitlement table on every query
- set **column rules** per user per table (an exact allow-list of columns) and **push** them —
  the console runs the `REVOKE` / `GRANT` statements for you and shows you exactly what it ran
- **preview** what any user would see before telling them
- one-click **setup** that creates the whole demo (sales table, entitlement table,
  row-filter function, security policy, masking) — idempotent, safe to re-run

No build step. Node + Express + vanilla JS. One `.env` file and it runs.

---

## 1. Prerequisites

| What | Why |
|---|---|
| Node.js 18+ | runs the app |
| Access to a Fabric workspace with a **Warehouse** | the POC target (create one named e.g. `wh_demo`) |
| **Azure CLI** (`az login`) *or* a service principal | Fabric only accepts Entra ID sign-in — there are no SQL passwords |

The identity the console signs in with is the **policy engine**: it needs to be able to create
tables and grant permissions, so use your own account (workspace Admin/Member) for the POC.

## 2. Setup

```bash
cp .env.example .env
# open .env and set:
#   FABRIC_SQL_SERVER   → Warehouse ▸ Settings ▸ SQL endpoint ▸ copy the connection string
#   FABRIC_SQL_DATABASE → the warehouse name (e.g. wh_demo)

az login            # sign in as yourself (skip if using a service principal in .env)
npm install
npm start           # → http://localhost:3000
```

Open the app, go to **Overview & setup**, click **Set up demo objects**. Done — the demo
warehouse objects exist and you're entitled to `All` regions so you keep sight of the data.

## 3. Using it

| Tab | What it does |
|---|---|
| **Overview & setup** | connection status, object checklist, one-click bootstrap |
| **Tables & columns** | browse every table/column; masked columns and current grants are marked |
| **Row rules** | the entitlement table as a form. Add `person@… → EU`. Applies instantly |
| **Column rules** | tick the exact columns a user may see, **Push rule** runs REVOKE+GRANT and shows the SQL |
| **Preview as user** | simulated result: regions, visible/struck-out columns, top-50 rows |

**Blush background = unpushed change** (same convention as the deck: blush means "new / not yet enforced").

## 4. The one thing SQL grants can't do

Grants control **what a user sees**; they don't let the user **connect**. Each test user still
needs the warehouse **shared** with them in the Fabric portal (Share → enter email → leave every
extra checkbox unticked). Do that once per user.

And keep test users out of workspace roles entirely — a workspace Viewer/Member/Admin
bypasses column rules by design. Item share + grants only.

## 5. Auth options

- **Option A (default)** — leave `AZURE_*` empty in `.env`, run `az login`. The app uses your
  Azure CLI session (Entra "default credential").
- **Option B** — service principal: fill `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`,
  `AZURE_CLIENT_SECRET`. The SPN must be allowed by the Fabric tenant setting
  ("Service principals can use Fabric APIs"), added to the workspace, and — to run this
  console — needs rights to create objects and grant permissions.

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| `Not connected — login failed / AADSTS…` | run `az login` with the right account; check tenant |
| `Failed to connect … getaddrinfo` | `FABRIC_SQL_SERVER` is wrong — copy the exact SQL connection string |
| Push fails with `Cannot find the user` | the console auto-creates users; if it still fails, share the warehouse with that user first |
| A test user connects but sees zero rows | no row rule for them yet — add one in **Row rules** |
| A test user can see everything | they hold a workspace role — remove it; use item share + grants only |
| Setup partially fails | re-run it; every step is idempotent and reports per-step errors |

## 7. Safety notes (POC scope)

- The console itself has **no login** — run it on localhost only. In the real build this is the
  authenticated Access Console from the proposal.
- All object names are validated against warehouse metadata and bracket-quoted before any DDL
  is composed; data values are parameterised. Emails are format-checked.
- Everything the console pushes is shown to you verbatim in the SQL log — the "policy engine"
  is transparent by design.

## 8. How this maps to the proposal

| Tenet | Here |
|---|---|
| 1 · Security at the data | RLS policy + column grants + masking on the warehouse |
| 3 · Business-owned admin | this console: constrained inputs, no free-text SQL |
| 4 · Entitlement as data | `gov.entitlement` — row rules are just rows |
| 6 · Assurance (seed) | Preview-as-user = the manual version of persona tests |

Project layout:

```
server/
  index.js            Express app + error handling
  db.js               Fabric SQL endpoint pool (Entra auth: az login or SPN)
  validate.js         identifier / email whitelisting
  routes/
    setup.js          health, status, idempotent bootstrap, preview
    meta.js           tables, columns (+ masked flags)
    entitlements.js   row rules CRUD + regions
    columnRules.js    read current grants, push REVOKE/GRANT
public/
  index.html, styles.css, app.js   the console UI (no framework, no build)
```

## 9. Verify (`npm run verify`)

A dependency-free harness (`scripts/verify.js`) that boots the server on a free port and proves
the access rules instead of assuming them. One line per check, then a summary; exit code 1 only
if something **FAIL**s.

- **Static checks** — always run, no `.env` needed: server boots, UI is served, bad emails and
  `]`-containing identifiers are rejected with 400, `/api/status` answers.
- **Warehouse checks** — need a connected `.env` (+ `az login`): setup runs twice and is fully ok,
  every object in `/api/status` is present, the operator is entitled to `All`.
- **Persona matrix** — needs `VERIFY_P3_EMAIL` + `VERIFY_P4_EMAIL` in `.env` (`VERIFY_P2_EMAIL`
  optional): **writes to the warehouse** — replaces those personas' row rules and their SELECT grants
  on `sales.orders` through the same API the UI uses — then checks what they see via `/api/preview` — **simulated** by the operator identity — including the
  headline flip EU → US and back.
- **SKIP** = could not be proven here, with the reason. **MANUAL** = print the exact SQL a human runs
  signed in as that persona (P2's denial, P3's masking, P4's column denial); never counted as a
  failure — masking and permission errors only show on a real sign-in.
