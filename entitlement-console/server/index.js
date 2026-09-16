require('dotenv').config();
const path = require('path');
const express = require('express');

const app = express();
app.use(express.json());
app.use(express.static(path.join(__dirname, '..', 'public')));

app.use('/api', require('./routes/setup'));
app.use('/api', require('./routes/meta'));
app.use('/api', require('./routes/entitlements'));
app.use('/api', require('./routes/columnRules'));
app.use('/api', require('./routes/changes'));

// Error handler: friendly messages, plus whatever SQL was executed before failure.
app.use((err, _req, res, _next) => {
  const status = err.status || 500;
  const body = { error: err.message || 'Unexpected error' };
  if (err.code === 'NOT_CONFIGURED') body.hint = 'Copy .env.example to .env and set FABRIC_SQL_SERVER / FABRIC_SQL_DATABASE.';
  if (/login|token|authentication|AADSTS/i.test(err.message || '')) {
    body.hint = 'Auth failed. Either run `az login` with an account that has access to the warehouse, or set the AZURE_* service-principal variables in .env.';
  }
  if (err.executed) body.executed = err.executed;
  res.status(status).json(body);
});

const port = process.env.PORT || 3000;
app.listen(port, () => {
  console.log(`Entitlement console → http://localhost:${port}`);
});
