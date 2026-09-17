const express = require('express');
const { q, authMode, getTarget, setTarget } = require('../db');
const { assertEmail, bracket, assertTableExists } = require('../validate');
const { logChange, changed } = require('../audit');
const { listWarehouses } = require('../fabric');

const router = express.Router();

// Idempotent bootstrap: each step guards its own existence.
const BOOTSTRAP = [
  { step: 'Schema sales', sql: `IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'sales') EXEC('CREATE SCHEMA sales')` },
  { step: 'Schema gov', sql: `IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'gov') EXEC('CREATE SCHEMA gov')` },
  { step: 'Schema sec', sql: `IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'sec') EXEC('CREATE SCHEMA sec')` },
  {
    step: 'Table sales.orders',
    sql: `IF OBJECT_ID('sales.orders','U') IS NULL
          CREATE TABLE sales.orders (
            order_id       int           NOT NULL,
            region         varchar(16)   NOT NULL,
            customer_name  varchar(64)   NOT NULL,
            customer_ssn   varchar(16)   NOT NULL,
            account_number varchar(34)   NOT NULL,
            amount         decimal(12,2) NOT NULL,
            order_date     date          NOT NULL
          )`,
  },
  {
    step: 'Seed sales.orders',
    sql: `IF NOT EXISTS (SELECT 1 FROM sales.orders)
          INSERT INTO sales.orders VALUES
           (1,'US','Ava Thompson','111-22-3333','US64SVBKUS6S3300958879',1200.00,'2026-09-01'),
           (2,'US','Noah Miller','222-33-4444','US12USBKUS44022235001', 560.50,'2026-09-03'),
           (3,'EU','Marie Dubois','333-44-5555','FR1420041010050500013M', 980.00,'2026-09-02'),
           (4,'EU','Lukas Weber','444-55-6666','DE89370400440532013000',1500.75,'2026-09-05'),
           (5,'APAC','Sanjay Rao','555-66-7777','IN60HDFC0000123456789', 720.25,'2026-09-04'),
           (6,'APAC','Mei Tanaka','666-77-8888','JP3501650000123456789', 430.00,'2026-09-06')`,
  },
  {
    // Migrate the old single-dimension schema (user_email, region) to the row-grant schema
    // (user_email, order_id). SCHEMABINDING forces the teardown order: policy → function → table.
    step: 'Migrate gov.entitlement (region → order_id)',
    sql: `IF COL_LENGTH('gov.entitlement','region') IS NOT NULL
          BEGIN
            IF EXISTS (SELECT 1 FROM sys.security_policies WHERE name = 'orders_rls') EXEC('DROP SECURITY POLICY sec.orders_rls');
            IF OBJECT_ID('sec.fn_rls_region','IF') IS NOT NULL EXEC('DROP FUNCTION sec.fn_rls_region');
            DROP TABLE gov.entitlement;
          END`,
  },
  {
    step: 'Table gov.entitlement',
    sql: `IF OBJECT_ID('gov.entitlement','U') IS NULL
          CREATE TABLE gov.entitlement (
            user_email varchar(256) NOT NULL,
            order_id   int          NOT NULL
          )`,
  },
  {
    step: 'Table gov.change_log',
    sql: `IF OBJECT_ID('gov.change_log','U') IS NULL
          CREATE TABLE gov.change_log (
            at     datetime2(3)  NOT NULL,
            actor  varchar(256)  NOT NULL,
            action varchar(32)   NOT NULL,
            detail varchar(4000) NOT NULL
          )`,
  },
  {
    // Row filter keyed on order_id: a user sees a row if they hold a grant for it, or the -1 "all rows" grant.
    step: 'Predicate sec.fn_rls_orders',
    sql: `IF OBJECT_ID('sec.fn_rls_orders','IF') IS NULL
          EXEC('CREATE FUNCTION sec.fn_rls_orders (@order_id int)
                RETURNS TABLE WITH SCHEMABINDING AS RETURN
                SELECT 1 AS ok FROM gov.entitlement e
                WHERE e.user_email = USER_NAME()
                  AND (e.order_id = @order_id OR e.order_id = -1)')`,
  },
  {
    // Idempotent AND convergent: create if missing, else force STATE = ON if a policy by that name
    // exists but is disabled — otherwise the IF NOT EXISTS guard would skip a policy left OFF and RLS
    // would stay inert (the row filter never applies). ALTER SECURITY POLICY is honored on Fabric.
    step: 'Security policy sec.orders_rls',
    sql: `IF NOT EXISTS (SELECT 1 FROM sys.security_policies WHERE name = 'orders_rls')
          EXEC('CREATE SECURITY POLICY sec.orders_rls
                ADD FILTER PREDICATE sec.fn_rls_orders(order_id) ON sales.orders
                WITH (STATE = ON)')
          ELSE IF EXISTS (SELECT 1 FROM sys.security_policies WHERE name = 'orders_rls' AND is_enabled = 0)
          ALTER SECURITY POLICY sec.orders_rls WITH (STATE = ON)`,
  },
  {
    step: 'Mask sales.orders.account_number',
    sql: `IF NOT EXISTS (SELECT 1 FROM sys.masked_columns mc
                         JOIN sys.objects o ON o.object_id = mc.object_id
                         JOIN sys.schemas s ON s.schema_id = o.schema_id
                         WHERE s.name = 'sales' AND o.name = 'orders' AND mc.name = 'account_number')
          EXEC('ALTER TABLE sales.orders ALTER COLUMN account_number
                ADD MASKED WITH (FUNCTION = ''partial(0,"XXXX-",4)'')')`,
  },
];

router.get('/health', (_req, res) => {
  const t = getTarget();
  res.json({
    configured: Boolean(t.server && t.database),
    server: t.server || null,
    database: t.database || null,
    authMode: authMode(),
  });
});

// List the signed-in admin's Fabric warehouses for the picker (lakehouses deferred).
router.get('/warehouses', async (_req, res, next) => {
  try {
    const t = getTarget();
    const list = await listWarehouses();
    const current = (w) => Boolean(t.server && w.server.toLowerCase() === t.server.toLowerCase() && w.database.toLowerCase() === (t.database || '').toLowerCase());
    res.json(list.map((w) => ({ ...w, current: current(w) })));
  } catch (err) { next(err); }
});

// Switch the active warehouse at runtime. Only a warehouse the admin actually has may be selected.
router.post('/target', async (req, res, next) => {
  try {
    const server = String(req.body.server || '').trim();
    const database = String(req.body.database || '').trim();
    if (!server || !database) return res.status(400).json({ error: 'server and database are required' });
    const list = await listWarehouses();
    const match = list.find((w) => w.server.toLowerCase() === server.toLowerCase() && w.database.toLowerCase() === database.toLowerCase());
    if (!match) return res.status(400).json({ error: 'That warehouse is not among your Fabric workspaces.' });
    await setTarget({ server, database });
    try {
      const me = (await q('SELECT USER_NAME() AS me')).recordset[0].me;
      res.json({ ok: true, server, database, connected: true, me });
    } catch (e) {
      res.json({ ok: true, server, database, connected: false, error: e.message });
    }
  } catch (err) { next(err); }
});

router.get('/status', async (_req, res) => {
  try {
    const me = (await q('SELECT USER_NAME() AS me')).recordset[0].me;
    const check = async (text) => (await q(text)).recordset[0].n > 0;
    const status = {
      connected: true,
      me,
      authMode: authMode(),
      objects: {
        salesOrders: await check(`SELECT COUNT(*) n FROM sys.tables t JOIN sys.schemas s ON s.schema_id=t.schema_id WHERE s.name='sales' AND t.name='orders'`),
        entitlement: await check(`SELECT COUNT(*) n FROM sys.tables t JOIN sys.schemas s ON s.schema_id=t.schema_id WHERE s.name='gov' AND t.name='entitlement'`),
        changeLog: await check(`SELECT COUNT(*) n FROM sys.tables t JOIN sys.schemas s ON s.schema_id=t.schema_id WHERE s.name='gov' AND t.name='change_log'`),
        policy: await check(`SELECT COUNT(*) n FROM sys.security_policies WHERE name='orders_rls' AND is_enabled=1`),
        masking: await check(`SELECT COUNT(*) n FROM sys.masked_columns mc JOIN sys.objects o ON o.object_id=mc.object_id WHERE o.name='orders' AND mc.name='account_number'`),
      },
    };
    if (status.objects.entitlement) {
      status.entitlementRows = (await q('SELECT COUNT(*) n FROM gov.entitlement')).recordset[0].n;
    }
    res.json(status);
  } catch (err) {
    res.json({ connected: false, error: err.message, code: err.code || null, authMode: authMode() });
  }
});

router.post('/setup', async (_req, res) => {
  const results = [];
  for (const s of BOOTSTRAP) {
    try {
      await q(s.sql);
      results.push({ step: s.step, ok: true });
    } catch (err) {
      results.push({ step: s.step, ok: false, error: err.message });
    }
  }
  // Give the connected identity 'All' so the console operator keeps sight of the data.
  let me, entitled = false;
  try {
    me = (await q('SELECT USER_NAME() AS me')).recordset[0].me;
    const r = await q(
      `IF NOT EXISTS (SELECT 1 FROM gov.entitlement WHERE user_email = @me AND order_id = -1)
       INSERT INTO gov.entitlement (user_email, order_id) VALUES (@me, -1)`,
      { me }
    );
    entitled = changed(r);
    results.push({ step: `Entitle ${me} to all rows`, ok: true });
  } catch (err) {
    results.push({ step: 'Entitle connected identity', ok: false, error: err.message });
  }
  // Evidence: the self-entitle only when it inserted a row; the run itself always. Never fails setup.
  try {
    if (entitled) await logChange('row-rule.add', `${me} → row ALL`);
    await logChange('setup.run', `${results.filter((r) => r.ok).length}/${results.length} steps ok`);
    results.push({ step: 'Write change log', ok: true });
  } catch (err) {
    results.push({ step: 'Write change log', ok: false, error: err.message });
  }
  res.json({ results, ok: results.every((r) => r.ok) });
});

// Simulated preview: what would this user see on this table?
router.get('/preview', async (req, res, next) => {
  try {
    const email = assertEmail(String(req.query.email || ''));
    const schema = String(req.query.schema || 'sales');
    const table = String(req.query.table || 'orders');
    await assertTableExists(schema, table);

    const allCols = (
      await q(
        `SELECT c.name FROM sys.columns c
         JOIN sys.tables t ON t.object_id = c.object_id
         JOIN sys.schemas s ON s.schema_id = t.schema_id
         WHERE s.name=@schema AND t.name=@table ORDER BY c.column_id`,
        { schema, table }
      )
    ).recordset.map((r) => r.name);

    const grants = (
      await q(
        `SELECT p.minor_id, c.name AS col
         FROM sys.database_permissions p
         JOIN sys.database_principals pr ON pr.principal_id = p.grantee_principal_id
         JOIN sys.objects o ON o.object_id = p.major_id
         JOIN sys.schemas s ON s.schema_id = o.schema_id
         LEFT JOIN sys.columns c ON c.object_id = p.major_id AND c.column_id = p.minor_id
         WHERE p.class = 1 AND p.permission_name = 'SELECT' AND p.state IN ('G','W')
           AND s.name = @schema AND o.name = @table AND pr.name = @email`,
        { schema, table, email }
      )
    ).recordset;

    const tableWide = grants.some((g) => Number(g.minor_id) === 0);
    const visibleColumns = tableWide ? allCols : grants.filter((g) => g.col).map((g) => g.col);

    // Row visibility comes from the entitlement table, keyed on order_id (-1 = all rows). The row
    // filter only applies to sales.orders (the one table the security policy is on); other tables
    // aren't row-filtered, so the user sees every row their column grants allow.
    const rlsTable = schema === 'sales' && table === 'orders';
    const ids = rlsTable
      ? (await q(`SELECT order_id FROM gov.entitlement WHERE user_email = @email`, { email })).recordset.map((r) => Number(r.order_id))
      : [-1];
    const hasAll = ids.includes(-1);
    const rowIds = ids.filter((id) => id !== -1);

    let rows = [];
    if (visibleColumns.length && (hasAll || rowIds.length)) {
      const colSql = visibleColumns.map((c) => bracket(c)).join(', ');
      const where = hasAll ? '' : `WHERE order_id IN (SELECT order_id FROM gov.entitlement WHERE user_email = @email AND order_id <> -1)`;
      rows = (
        await q(`SELECT TOP 50 ${colSql} FROM ${bracket(schema)}.${bracket(table)} ${where}`, { email })
      ).recordset;
    }

    res.json({
      simulated: true,
      note: 'Computed from grants + entitlement rows by the console identity. Masking is not simulated — a real sign-in by this user would also see masked values.',
      email, schema, table,
      allRows: hasAll,
      rowIds: hasAll ? 'all' : rowIds,
      visibleColumns,
      hiddenColumns: allCols.filter((c) => !visibleColumns.includes(c)),
      rows,
    });
  } catch (err) { next(err); }
});

module.exports = router;
