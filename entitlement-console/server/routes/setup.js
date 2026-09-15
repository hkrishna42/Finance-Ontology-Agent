const express = require('express');
const { q, authMode } = require('../db');
const { assertEmail, bracket, assertTableExists } = require('../validate');

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
    step: 'Table gov.entitlement',
    sql: `IF OBJECT_ID('gov.entitlement','U') IS NULL
          CREATE TABLE gov.entitlement (
            user_email varchar(256) NOT NULL,
            region     varchar(16)  NOT NULL
          )`,
  },
  {
    step: 'Predicate sec.fn_rls_region',
    sql: `IF OBJECT_ID('sec.fn_rls_region','IF') IS NULL
          EXEC('CREATE FUNCTION sec.fn_rls_region (@region varchar(16))
                RETURNS TABLE WITH SCHEMABINDING AS RETURN
                SELECT 1 AS ok FROM gov.entitlement e
                WHERE e.user_email = USER_NAME()
                  AND (e.region = @region OR e.region = ''All'')')`,
  },
  {
    step: 'Security policy sec.orders_rls',
    sql: `IF NOT EXISTS (SELECT 1 FROM sys.security_policies WHERE name = 'orders_rls')
          EXEC('CREATE SECURITY POLICY sec.orders_rls
                ADD FILTER PREDICATE sec.fn_rls_region(region) ON sales.orders
                WITH (STATE = ON)')`,
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
  res.json({
    configured: Boolean(process.env.FABRIC_SQL_SERVER && process.env.FABRIC_SQL_DATABASE),
    server: process.env.FABRIC_SQL_SERVER || null,
    database: process.env.FABRIC_SQL_DATABASE || null,
    authMode: authMode(),
  });
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
  try {
    const me = (await q('SELECT USER_NAME() AS me')).recordset[0].me;
    await q(
      `IF NOT EXISTS (SELECT 1 FROM gov.entitlement WHERE user_email = @me AND region = 'All')
       INSERT INTO gov.entitlement (user_email, region) VALUES (@me, 'All')`,
      { me }
    );
    results.push({ step: `Entitle ${me} to All`, ok: true });
  } catch (err) {
    results.push({ step: 'Entitle connected identity', ok: false, error: err.message });
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

    const tableWide = grants.some((g) => g.minor_id === 0);
    const visibleColumns = tableWide ? allCols : grants.filter((g) => g.col).map((g) => g.col);

    const regions = (
      await q(`SELECT region FROM gov.entitlement WHERE user_email = @email`, { email })
    ).recordset.map((r) => r.region);
    const hasAll = regions.includes('All');

    let rows = [];
    if (visibleColumns.length && (regions.length || hasAll)) {
      const colSql = visibleColumns.map((c) => bracket(c)).join(', ');
      const where = hasAll ? '' : `WHERE region IN (SELECT region FROM gov.entitlement WHERE user_email = @email)`;
      rows = (
        await q(`SELECT TOP 50 ${colSql} FROM ${bracket(schema)}.${bracket(table)} ${where}`, { email })
      ).recordset;
    }

    res.json({
      simulated: true,
      note: 'Computed from grants + entitlement rows by the console identity. Masking is not simulated — a real sign-in by this user would also see masked values.',
      email, schema, table,
      regions: hasAll ? ['All'] : regions,
      visibleColumns,
      hiddenColumns: allCols.filter((c) => !visibleColumns.includes(c)),
      rows,
    });
  } catch (err) { next(err); }
});

module.exports = router;
