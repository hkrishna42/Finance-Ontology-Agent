const { q } = require('./db');

// Evidence trail: every mutation the console makes lands in gov.change_log.
// Actor is USER_NAME() on the warehouse side — never client-supplied.
const AUDIT_SQL = `INSERT INTO gov.change_log (at, actor, action, detail) VALUES (GETUTCDATE(), USER_NAME(), @action, @detail)`;

async function logChange(action, detail) {
  // varchar(4000) is 4000 bytes under the UTF-8 collation: keep a margin and mark the cut.
  const text = String(detail);
  try {
    await q(AUDIT_SQL, { action, detail: text.length > 3900 ? text.slice(0, 3900) + '\n-- [truncated]' : text });
  } catch (err) {
    throw new Error(`Change applied but not logged (${err.message.replace(/\.$/, '')}) — run setup on the Overview page to create gov.change_log, or grant INSERT on it to this identity.`);
  }
}

// mssql's rowsAffected is one count per statement that reported one; a skipped IF ... INSERT reports none.
const changed = (r) => r.rowsAffected.reduce((a, b) => a + b, 0) > 0;

module.exports = { logChange, changed, AUDIT_SQL };
