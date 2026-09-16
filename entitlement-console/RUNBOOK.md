# Runbook — running the Entitlement Console on a fresh machine

Step-by-step from an empty machine to the live demo. Any OS with Node works (Mac, Windows, Linux);
no Docker, no build step. The console signs in **as you** and acts as the policy engine, so the
account you use must be able to create tables and grant permissions on the warehouse.

## 0. One-time prerequisites on the machine

| What | How to check / install |
|---|---|
| Node.js 18+ | `node --version` |
| Azure CLI | Mac `brew install azure-cli` · Windows `winget install Microsoft.AzureCLI` · then `az --version` |
| git + this repo | `git clone https://github.com/hkrishna42/Finance-Ontology-Agent.git` (or `git pull --ff-only` on an existing clone) |

## 1. One-time prerequisites in Fabric (portal, by you)

1. A workspace where **your account is Admin**.
2. A **Warehouse** in it (e.g. `wh_demo`). Not a lakehouse — its SQL analytics endpoint is read-only.
3. Warehouse → **Settings → SQL endpoint** → copy the connection string
   (looks like `xxxxxxxx.datawarehouse.fabric.microsoft.com`).

## 2. Configure

```bash
cd entitlement-console
cp .env.example .env
```

Edit `.env`:

- `FABRIC_SQL_SERVER` = the string from step 1.3
- `FABRIC_SQL_DATABASE` = the warehouse name (e.g. `wh_demo`)
- leave `AZURE_TENANT_ID` / `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` empty — you sign in as yourself
- leave `VERIFY_P*_EMAIL` empty for now (step 6)

`.env` is git-ignored. Never commit it.

## 3. Sign in and start

```bash
az login
npm ci
npm start
```

Open <http://localhost:3000>. The navy status bar should read
*Connected to wh_demo as you@… · policy not installed*.

If it says **Not connected**, the error text tells you which of these it is:

| Message contains | Fix |
|---|---|
| `FABRIC_SQL_SERVER / FABRIC_SQL_DATABASE not set` | `.env` missing or not filled in |
| `getaddrinfo ENOTFOUND` | wrong `FABRIC_SQL_SERVER` |
| `login` / `token` / `AADSTS` | `az login` not done, or done with an account that has no access to the warehouse |
| `Cannot open database` | wrong `FABRIC_SQL_DATABASE` |
| `Connection lost - socket hang up` | the gateway closed the connection mid-login. Run `node scripts/diag-connect.js` — it tries the app's connection plus two variants and prints the driver's state log; send that output |

## 4. Bootstrap the demo

**Overview & setup → Set up demo objects.** Idempotent — safe to click again at any time.
It creates `sales.orders` (6 fictional rows across US/EU/APAC), `gov.entitlement`,
`gov.change_log`, the row-level-security predicate and policy, masking on `account_number`,
and entitles *you* to `All` (the RLS filter applies to admins too — without this row you would
see nothing). All five checklist rows turn green.

## 5. Prove it — the harness

```bash
npm run verify
```

With only `.env` + `az login` you get the **static** suite and the **warehouse** suite
(setup runs twice and is fully ok, every object present, you are entitled to `All`).
`0 failed` is the goal; `SKIP` rows say what could not be proven and why.

## 6. Personas — the human steps

The console pushes grants; it cannot make a user able to connect. For each test user
(P2 access admin, P3 analyst EU, P4 analyst APAC):

1. Fabric portal → the warehouse → **Share** → their email → **leave every extra checkbox unticked**.
2. Give them **no workspace role**. Admin/Member/Contributor bypass column security by design,
   and Viewer gets ReadData and bypasses it too.
3. Put their emails in `.env`: `VERIFY_P2_EMAIL`, `VERIFY_P3_EMAIL`, `VERIFY_P4_EMAIL`
   (real Entra users; they must be distinct and not your own account).

Then:

```bash
npm run verify
```

The **persona matrix** now runs: P3 → EU + all columns, P4 → APAC minus `customer_ssn`,
P2 → no rows and no columns, each checked through the simulated preview, plus the headline
**EU → US flip and restore** for P3. Expect `0 failed`.

The `MANUAL` rows at the end are what only a real sign-in can prove. Each persona opens the
warehouse as themselves (Fabric SQL query editor or SSMS) and runs the printed SQL:

| Persona | Runs | Expects |
|---|---|---|
| P1 (you) | `SELECT COUNT(*) FROM sales.orders` | 6 |
| P2 | `SELECT * FROM sales.orders` | error 229, permission denied |
| P2 | `INSERT INTO gov.entitlement VALUES ('x@example.com','EU')` | succeeds — after you run the printed `GRANT SELECT, INSERT, UPDATE, DELETE ON OBJECT::gov.entitlement TO [p2 email]` |
| P3 | `SELECT account_number FROM sales.orders` | 2 EU rows, values begin `XXXX-` |
| P4 | `SELECT customer_ssn FROM sales.orders` | error 230, column permission denied |

## 7. The live demo moment

**Row rules** tab → remove P3's `EU` row → add P3 `US`. P3 re-runs
`SELECT * FROM sales.orders` and gets the two US rows. No redeploy, no policy edit — one row
changed in a table. **Evidence** tab shows both changes with who, when and what.

## Things that trip people

- **Grants ≠ share.** A user with perfect grants still cannot connect until step 6.1 is done.
- **Case matters.** Fabric's default collation is case-sensitive. Enter each user's email exactly
  as `SELECT USER_NAME()` returns it for *them*; otherwise the RLS row silently fails to match and
  they see zero rows.
- **Masking looks "off" for you.** `db_owner`-mapped identities (you) see real `account_number`
  values; test users see `XXXX-…`. Expected.
- **Setup is safe to re-run**, but it will not turn the security policy back on if someone has
  set it to `STATE = OFF` by hand — the Overview checklist shows it red in that case.
