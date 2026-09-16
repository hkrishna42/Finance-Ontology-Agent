# Configurable entitlement dimension — decision note

Branch `ec/configurable-dimension` · design only, no code · for the operator · status: waiting on the decisions in §8.

## 1. Summary

- `gov.entitlement(user_email, region)` becomes `gov.entitlement(user_email, dimension, value)`, plus a new `gov.binding` that records which column of which table each dimension governs.
- Why: "region" is baked into the table, the predicate, the policy, three API routes and three views. A second dimension (business unit, desk, country) today means forking all of them.
- Recommended: option A — one generated `WITH SCHEMABINDING` function per dimension, one security policy per bound table, DDL generated only from whitelisted identifiers and run one statement per batch (never through `EXEC`); a combined per-table predicate later for tables bound to several dimensions.
- It alters the demo schema (the predicate and policy are dropped and recreated, the entitlement table is reshaped), so it is your call. The defaults in §8 are picked so that phase 1 changes nothing a demo viewer can see.
- Nothing here has been run against the warehouse; §7 lists what to verify before any code.

## 2. Today

One dimension, hard-coded end to end. `gov.entitlement` has a single data column, `region`; the predicate `sec.fn_rls_region` reads that column by name; the policy feeds `sales.orders.region` into it; the console's routes and views use "region" as a SQL column, a JSON field, a route path (`/regions`) and UI labels. The success test (change one row, two users see different data) holds — for that one column of that one table.

| Object / coupling | Where |
|---|---|
| `gov.entitlement(user_email varchar(256), region varchar(16))` | `server/routes/setup.js:36-43` |
| `sec.fn_rls_region(@region)` `WITH SCHEMABINDING`; matches `USER_NAME()`; honors the literal `'All'` | `setup.js:44-52` |
| `sec.orders_rls` — `ADD FILTER PREDICATE sec.fn_rls_region(region) ON sales.orders`, STATE = ON | `setup.js:53-59` |
| Operator self-entitled to `'All'` (RLS applies to admins) | `setup.js:114-125` |
| `/status` looks for the policy by the literal name `orders_rls` | `setup.js:91` |
| `/preview` reads `region` rows, then filters with a hard-coded `WHERE region IN (SELECT region FROM gov.entitlement …)` | `setup.js:164-176` (the WHERE: `:172`) |
| `/entitlements` GET/POST/DELETE — `region` in SQL and JSON; POST validates against `SELECT DISTINCT region FROM sales.orders` | `server/routes/entitlements.js:7-12, 24-40, 42-49` |
| `/regions` — DISTINCT of `sales.orders.region` + `'All'` | `entitlements.js:14-21` |
| Row rules view: `<select id="rRegion">` fed by `/regions`; rows keyed on `e.region`; add/remove send `region` | `public/app.js:143, 153-159, 164, 174` |
| Preview view: `r.regions` rendered as chips | `app.js:272` |
| Docs: "who sees which regions", personas defined by region | `README.md:7, 52`; `CLAUDE.md:94-107` |

Column rules (`server/routes/columnRules.js`, `app.js:183-251`) never mention region and are untouched by this note.

## 3. Proposed schema

```sql
IF OBJECT_ID('gov.entitlement','U') IS NULL
CREATE TABLE gov.entitlement (
  user_email varchar(256) NOT NULL,
  dimension  varchar(32)  NOT NULL,   -- console-normalized: ^[a-z][a-z0-9_]{0,31}$
  value      varchar(128) NOT NULL    -- exact match; the literal 'All' is the wildcard
);
IF OBJECT_ID('gov.binding','U') IS NULL
CREATE TABLE gov.binding (
  schema_name varchar(128) NOT NULL,
  table_name  varchar(128) NOT NULL,
  column_name varchar(128) NOT NULL,
  dimension   varchar(32)  NOT NULL
);
```

`gov.binding` is the console's map: "dimension `region` governs `sales.orders.region`". Setup seeds that one row. A dimension bound to two tables has two rows; a table bound to two dimensions has two rows (phase 3).

Fabric warehouse constraints that shape this:

| Constraint | Consequence |
|---|---|
| No IDENTITY, no DEFAULT constraints (per Fabric docs — verify: `CREATE TABLE scratch(c int DEFAULT 0)`, §7 #4) | no surrogate ids, no `created_at DEFAULT …` — the console supplies every value |
| PRIMARY KEY / UNIQUE only as NOT ENFORCED | duplicates are prevented by the console's `IF NOT EXISTS … INSERT` (as today), not by the warehouse |
| Default collation `Latin1_General_100_BIN2_UTF8`, case-sensitive | `'region'` ≠ `'Region'`, `'EU'` ≠ `'eu'`, `'All'` ≠ `'ALL'`. The console lower-cases and regex-checks dimension names; values are never typed by a person — they come from the bound column's DISTINCT list, so they match by construction |
| No user-created indexes | nothing to tune on `gov.entitlement`; it stays tiny (users × dimensions) |
| ALTER TABLE column operations limited (verify, §7 #4) | reshaping `gov.entitlement` is a recreate, not an ALTER — see §5 |

`'All'` carries over per dimension: `(p3, 'region', 'All')` means every region and says nothing about any other dimension. There is no global wildcard. The operator therefore holds one `'All'` row per dimension: setup writes it for `region`; every later bind writes it for the new dimension, or the operator loses sight of that table.

## 4. Predicate strategy

**A. One generated function per dimension.** `sec.[fn_rls_<dimension>](@value)` `WITH SCHEMABINDING`; the body hard-codes the dimension as a literal. The console generates the DDL from the dimension name (regex-whitelisted) and from the bound column (`assertTableExists` / `assertColumnsExist`, then `bracket()` — `server/validate.js`). One security policy per bound table, one filter predicate in it. Each generated statement runs as its own `q()` batch — never inside `EXEC('…')`, where the escape character is `'`, which `bracket()` does not reject (it rejects only `]`) and the `sys.*` whitelist does not exclude (an object named `[it's]` is legal). Today no whitelisted identifier is interpolated into an `EXEC` string (BOOTSTRAP's `EXEC` bodies are constants); this keeps it that way. `db.js` sends one batch per `q()` call, which also satisfies `CREATE FUNCTION`'s must-be-first-in-batch rule. The existence guards become separate parameterized queries.

```sql
-- guard, parameterized:  SELECT OBJECT_ID(@fn, 'IF')                                @fn = 'sec.fn_rls_region'
-- if missing → run, as its own batch:
CREATE FUNCTION sec.[fn_rls_region] (@value varchar(128))
RETURNS TABLE WITH SCHEMABINDING AS RETURN
SELECT 1 AS ok FROM gov.entitlement e
WHERE e.user_email = USER_NAME()
  AND e.dimension = 'region'
  AND (e.value = @value OR e.value = 'All')

-- guard, parameterized:  SELECT 1 FROM sys.security_policies WHERE name = @policy   @policy = 'rls_sales_orders'
-- if missing → run, as its own batch:
CREATE SECURITY POLICY sec.[rls_sales_orders]
ADD FILTER PREDICATE sec.[fn_rls_region]([region]) ON [sales].[orders]
WITH (STATE = ON)
```

The dimension literal in the function body (`'region'`) is now the only generated text inside a quoted literal, and the regex `^[a-z][a-z0-9_]{0,31}$` is the sole defense there. It is re-applied whenever a dimension is read back from `gov.binding` — decision 5 allows for hand-inserted rows — and never trusted from the table.

Trade-offs: D functions + T policies. A function is shared by every table bound to its dimension, so binding or unbinding a table touches one policy and nothing shared. Cost: the console now generates function DDL (today it only generates GRANT/REVOKE). The parameter is `varchar(128)` for every dimension; phases 1–2 accept only `varchar` columns with `max_length ≤ 128` at bind time — an `nvarchar` → `varchar` conversion, or truncation of a longer value into `@value varchar(128)`, can make distinct values compare equal (int keys: verify implicit conversion, phase 3).

**B. One generic function** `sec.fn_rls(@dimension, @value)`, the dimension passed as a constant from the policy: `ADD FILTER PREDICATE sec.fn_rls('region', [region]) ON …`. Saves the function generator (policies are still generated per table). Whether `CREATE SECURITY POLICY` accepts a literal argument next to a column **must be verified on Fabric before this option is even considered** — this note does not assume it either way. Also: one shared function means any change to its body requires dropping or detaching (`ALTER SECURITY POLICY … DROP FILTER PREDICATE`) every predicate that references it first (the reference lock), and one parameter type serves all dimensions.

**C. Tables bound to two dimensions.** Each (policy, table) pair carries one filter predicate, so AND-across-dimensions needs either (i) a combined generated predicate — A-style, one function per *table* taking N column arguments, one `EXISTS` clause per dimension — or (ii) one policy per dimension, meaning two enabled policies filtering the same table; SQL Server's `CREATE SECURITY POLICY` remarks say multiple active policies cannot contain predicates on the same table, and Fabric must be checked (§7 #2). OR-semantics has only one shape: the combined predicate. Route (i) needs nothing new from Fabric.

```sql
CREATE FUNCTION sec.[fn_rls_sales_orders] (@region varchar(128), @business_unit varchar(128))
RETURNS TABLE WITH SCHEMABINDING AS RETURN
SELECT 1 AS ok
WHERE EXISTS (SELECT 1 FROM gov.entitlement e WHERE e.user_email = USER_NAME()
              AND e.dimension = 'region' AND (e.value = @region OR e.value = 'All'))
  AND EXISTS (SELECT 1 FROM gov.entitlement e WHERE e.user_email = USER_NAME()
              AND e.dimension = 'business_unit' AND (e.value = @business_unit OR e.value = 'All'))
```

**Recommendation: A**, with the combined per-table predicate added in phase 3. It is the pattern already running on this warehouse (literal in the body, one column argument in the policy), so phases 1–2 depend on no unverified Fabric behavior; the only generated text inside a quoted literal is the dimension name, held to the regex — every other generated token is a `bracket()`-quoted identifier in a statement that runs as its own batch, never inside an `EXEC` string; binding a table is one policy and unbinding is one DROP, nothing shared is edited; B hinges on an unverified feature and concentrates every dimension in one function that cannot be altered without dropping or detaching every predicate that references it; the phase-3 generator is A's generator with N arguments, not a second mechanism.

## 5. What changes, concretely

| Area | Change |
|---|---|
| `gov.entitlement` DDL + data | New shape (§3). Existing rows map `region → (dimension='region', value=region)`. Recommended for a POC: re-run setup on a fresh warehouse. In place: copy rows to a scratch table, drop, create the new shape, copy back with `'region'` as the dimension, drop the scratch table — a short guarded script, run once by hand, not part of `/setup`. Compare row counts before the drop. |
| `sec.fn_rls_region` (SCHEMABINDING) | Body filters on `e.dimension = 'region'` and compares `e.value`. SCHEMABINDING blocks dropping `gov.entitlement` or changing the columns the function reads (not every ALTER — `ADD COLUMN` is allowed on SQL Server, Fabric per §7 #3–4), and the policy blocks dropping its function, so the reshape runs `DROP SECURITY POLICY` → `DROP FUNCTION` → reshape/recreate `gov.entitlement` → `CREATE FUNCTION` → `CREATE SECURITY POLICY`. Between the first and the last step `sales.orders` is unfiltered for anyone holding SELECT — run it while no demo user is querying. This chain wraps the hand-run script from the row above and, like it, never goes into `/setup`; it is constant text, so `EXEC('…')` guards are fine there. Every step keeps its guard (`IF EXISTS … DROP`, `IF … IS NULL CREATE`), so a re-run after a partial failure resumes where it stopped, and `/setup` on an already-migrated warehouse is a no-op. |
| Security policy | One per bound table, generated name `sec.[rls_<schema>_<table>]`. The derived name can collide (`(a, b_c)` vs `(a_b, c)`) and can exceed 128 chars: reject a name longer than 128 or one already present in `sys.security_policies` targeting a different object — or derive it from `object_id`. `sec.orders_rls` goes in the migration and `sec.[rls_sales_orders]` replaces it. `/status` stops looking for the literal `orders_rls` and checks that every `gov.binding` row has an enabled policy. |
| `setup.js` BOOTSTRAP | The legacy-shape check runs **first** (`sys.columns` shows `region` on `gov.entitlement`) and **aborts** the run with "migrate or use a fresh warehouse". It must abort rather than skip: an un-migrated warehouse has the old `sec.fn_rls_region` under the same name, so `OBJECT_ID(@fn, 'IF')` cannot tell the old body from the new, and a run that continued would add a second policy on `sales.orders` and `INSERT` into `dimension` / `value` columns that do not exist. Then: `gov.entitlement` (new shape) · `gov.binding` + seed row `(sales, orders, region, region)` · function for `region` · self-entitle `(me, 'region', 'All')` · policy for `sales.orders`. Function and policy DDL come from one generator shared with `/bindings`, so setup is "push the seed binding". |
| API | `/regions` → `/dimensions` (DISTINCT from `gov.binding`) and `/values?dimension=` (DISTINCT of every column bound to that dimension, unioned, + `'All'`). `/entitlements` GET/POST/DELETE gain `dimension`; POST rejects a dimension with no binding (the row would enforce nothing) and a value outside `/values`. New `GET /bindings` (rows + `enforced`, computed from `sys.security_policies`) and `POST /bindings {schema, table, column, dimension}`: whitelist; reject a table that already has a binding (one filter predicate per (policy, table) until phase 3 — otherwise the push writes the `gov.binding` row, fails at the policy, and leaves drift); write the row; create the function if missing; self-entitle the operator `'All'` on that dimension — a plain `INSERT`, **before** the policy, so there is no lock-out window (`/preview`'s SELECT runs as the operator, so operator RLS would otherwise silently shrink every preview); create the policy; return `executed`. `DELETE /bindings` drops the policy and the row; the function stays (harmless, reused on re-bind). `/preview` reads `gov.binding` for the table and builds one `[col] IN (SELECT e.value FROM gov.entitlement e WHERE e.user_email = @email AND e.dimension = @dN)` clause per bound dimension the user does not hold `'All'` on, ANDed — replacing the hard-coded WHERE at `setup.js:172`. Response carries `entitlements: { region: ['EU'] }` instead of `regions` (phase 2; phase 1 keeps `regions`). |
| UI — Row rules | Dimension selector (`/dimensions`) beside the email; the value list reloads from `/values?dimension=`; the table gains a Dimension column; Remove sends `dimension`. |
| UI — Tables & columns | For the selected table: bound dimensions listed; a "bind column → dimension" row (column select, dimension input). Choosing turns the panel blush (`.pending`, the same toggle as `app.js:210-215`) until "Push binding" runs `POST /bindings` and shows `executed`. Blush stays reserved for not-yet-enforced, so a binding row whose policy is missing (`enforced:false`) also renders blush. |
| UI — Preview | One chip group per dimension (the `entitlements` keys), an `'All'` chip where held. |
| UI — Column rules | Unchanged. |
| Verify harness (`npm run verify`, sibling branch) | Integration point only. Phase 1 keeps the JSON shapes, so it passes unchanged; phase 2 renames `/regions` and adds `dimension` to `/entitlements`, and its assertions move in the same commit. |
| Audit trail (`gov.change_log`, sibling branch) | Integration point only. `POST/DELETE /bindings` write `binding.push` / `binding.drop` beside the existing entitlement actions. |

## 6. Ground rules that must survive

- [ ] `gov.entitlement` and the predicate stay in the same database — SCHEMABINDING requires it; `gov.binding` lives there too.
- [ ] Demo users keep zero workspace roles; item share + SQL grants only. The UI warning stays.
- [ ] Grants ≠ share: the console still cannot make a user able to connect.
- [ ] RLS applies to admins → the operator is self-entitled to `'All'` on **every** dimension: at setup for `region`, inside every `POST /bindings` for the new one — before its policy is created.
- [ ] Warehouse only; the lakehouse SQL endpoint is read-only and out of scope.
- [ ] Masking vs `db_owner` unchanged, and still not simulated by Preview — the note stays honest.
- [ ] Identifiers whitelisted (`assertTableExists` / `assertColumnsExist`, dimension regex) then `bracket()`-quoted; every data value (`@email`, `@dimension`, `@value`) parameterized. Generated DDL runs one statement per batch, never inside an `EXEC('…')` string, so no identifier ever sits inside a quoted literal. The dimension name is the one generated string that appears inside a quoted literal at all (the function body) as well as in an identifier (the function-name suffix) — which is why its whitelist is a strict regex, not `bracket()` alone, re-applied on every read from `gov.binding`.
- [ ] Every executed statement is appended to `executed` and rendered verbatim in a `.sqllog` block — bindings included.
- [ ] All DDL idempotent; `/setup` safe to re-run.

## 7. Unknowns to verify on the warehouse first

| # | Question | One-line test |
|---|---|---|
| 1 | Does `CREATE SECURITY POLICY` accept a literal argument beside a column? (decides whether B is possible at all) | On a scratch table: `ADD FILTER PREDICATE sec.fn_probe('x', [col])` — success or error |
| 2 | Can two enabled policies each filter the same table? SQL Server's docs say no (decides whether C-ii exists, and how long phase 2's one-binding-per-table rule stays) | Create two policies on the scratch table, both with a filter predicate on it |
| 3 | Does `DROP FUNCTION` fail while a policy references it, and which `ALTER TABLE gov.entitlement` operations (`ADD` a column vs. `ALTER` / `DROP` a referenced one) fail while the SCHEMABINDING function exists? (confirms the §5 order) | Try each against the current objects on a throwaway warehouse |
| 4 | Are DEFAULT constraints really absent, which `ALTER TABLE` column operations exist, and is `sp_rename` supported for tables? (whether in-place migration can avoid copy-out / copy-back) | `CREATE TABLE scratch(c int DEFAULT 0)`; `ALTER TABLE scratch ADD c2 varchar(8) NULL`; `EXEC sp_rename 'scratch', 'scratch2'` |
| 5 | Is a function body readable back (`sys.sql_modules` / `OBJECT_DEFINITION`)? (lets `/bindings` skip a recreate when the definition is unchanged; otherwise: recreate only when missing) | `SELECT OBJECT_DEFINITION(OBJECT_ID('sec.fn_rls_region'))` |
| 6 | What does `USER_NAME()` return for a shared Entra user, with which casing, vs `sys.database_principals.name` and the email typed into the console? | P3 runs `SELECT USER_NAME()`; compare byte-for-byte with the `gov.entitlement` row |
| 7 | Collation of the warehouse as created (BIN2 vs CI)? | `SELECT DATABASEPROPERTYEX(DB_NAME(), 'Collation')` |
| 8 | Is DDL allowed inside an explicit transaction? (would make the drop → create chain atomic and close the unfiltered window) | `BEGIN TRAN; CREATE SECURITY POLICY …; ROLLBACK` on scratch objects |
| 9 | Are `sys.security_predicates` rows (predicate text, target object) available? (drift check: policies vs `gov.binding`) | `SELECT * FROM sys.security_predicates` |
| 10 | Predicate cost on a table that is not six rows, and with two `EXISTS` clauses (phase 3) | Copy `sales.orders` ×100k into a scratch table, bind it, compare elapsed with the policy ON vs OFF |

## 8. Decisions for the operator

1. **AND or OR across dimensions for one user?** Default: AND (least privilege — EU *and* business unit X). Consequence: binding a second dimension to a table hides it from every user without a rule on that dimension; the bind action shows the count of affected users and does nothing automatic.
2. **`'All'` per dimension, no global wildcard?** Default: yes. Easy to explain, and it is what the predicate literally does.
3. **Keep the column name `region` in `sales.orders`?** Default: yes. The data column is data; only the entitlement side generalizes. Renaming it churns seed rows, personas, README and the harness for nothing.
4. **Migrate in place or re-run setup on a fresh warehouse?** Default: fresh (POC). The manual script from §5 covers the case where the current warehouse must survive; it does not go into `/setup`.
5. **Is `gov.binding` edited only through the console?** Default: yes. A hand-inserted row is inert (no policy) and shows blush / not enforced; a hand-made policy without a row is invisible to Preview. `GET /bindings` reports both kinds of drift once §7 #9 is confirmed.
6. **Dimension name and value rules?** Default: name matches `^[a-z][a-z0-9_]{0,31}$` (lower-cased by the console); values exact-match and come only from the bound column; bindable columns are `varchar` with `max_length ≤ 128` only until phase 3.

## 9. Rollout

**P1 — schema + function generalization, `region` only, zero UI change.** New `gov.entitlement` shape, `gov.binding` with the seed row, regenerated function and policy (new policy name), self-entitle `(me, 'region', 'All')`, legacy-shape check first in `/setup` (abort) and reported by `/status`. `/regions`, `/entitlements` and `/preview` keep their JSON shapes (`regions` included) by reading `dimension = 'region'` internally. Behavior-preserving on a fresh or migrated warehouse — the demo behaves exactly as before and the harness passes unchanged; on the current warehouse `/setup` refuses until the §5 script has run. Shippable alone.

**P2 — bindings API + Row-rules dimension selector.** `/dimensions`, `/values`, `/entitlements` with `dimension`, `/bindings` GET/POST/DELETE, generator shared with setup, Row rules and Tables & columns views, Preview chips per dimension and the `entitlements: { region: [...] }` response (replacing `regions`), README / CLAUDE.md wording, harness assertions, `binding.*` audit actions. First moment a second dimension can be bound — to a *different* table only: `POST /bindings` rejects a table that already has a binding until phase 3 (one filter predicate per (policy, table); SQL Server documents that multiple active policies cannot predicate the same table, Fabric unverified — §7 #2).

**P3 — multi-dimension per table.** Combined per-table predicate (N arguments, AND), bind-time warning with the affected-user count, other column types (`nvarchar`, `varchar` > 128, int keys), drift report. Needs §7 #2 and #10 answered.

## 10. Risks

- **Predicate cost.** Every query on a bound table evaluates the predicate per row; phase 3 adds a subquery per dimension. `gov.entitlement` stays small, but measure (§7 #10) before binding a real table.
- **Collation / case.** BIN2 makes every comparison exact. Covered for dimensions and values (never typed by a person); not covered for emails — next item.
- **`USER_NAME()` casing.** If the principal name and the entitlement row differ in case, the user silently sees zero rows. This is today's exposure, unchanged by the design; fix once §7 #6 is known (normalize on write, or `LOWER()` both sides in the predicate).
- **Data migration.** In-place reshaping is copy-out / drop / copy-back with no transaction guarantee (§7 #8); a failure between the drop and the copy-back loses rules. A fresh warehouse avoids it; otherwise count rows before and after.
- **Unfiltered window.** Between `DROP SECURITY POLICY` and `CREATE SECURITY POLICY` the table is open to anyone with SELECT. Milliseconds on a demo; still, run it while no demo user is querying.
- **Default deny on bind.** A new dimension on an already-shared table hides it from everyone without a rule. The operator's `'All'` row is inserted before the policy is created, so a failed self-entitle stops the push before anything is enforced — and before `/preview`, which SELECTs as the operator, starts silently shrinking. The push logs every statement in `executed`; read the log.
- **Things that still say `region`.** README intro, §2, §3 and the file tree; Overview copy "All regions" (`app.js:82`); Row-rules copy; CLAUDE.md API surface, personas and verification lines; the harness; and any demo script that flips "EU → US". Phase 2 touches all of them in one commit.
- **Drift.** `gov.binding` and `sys.security_policies` can disagree after a failed push or a hand edit; until the drift report exists, `enforced` on `GET /bindings` is the only tell.
