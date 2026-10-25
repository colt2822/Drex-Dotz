-- Operational transport buffer only. PHOENIX CRM remains the attribution and revenue authority.
CREATE TABLE IF NOT EXISTS phoenix_ac_client (
 token_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL UNIQUE, source TEXT NOT NULL,
 campaign TEXT NOT NULL, synthetic INTEGER NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS phoenix_ac_event (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, client_id TEXT,
 stage TEXT NOT NULL, source TEXT NOT NULL, campaign TEXT NOT NULL,
 tool TEXT, success INTEGER NOT NULL, synthetic INTEGER NOT NULL,
 evidence_class TEXT NOT NULL DEFAULT 'UNVERIFIED_PUBLIC', elapsed_ms REAL,
 provider_cost_usd REAL, payment_status TEXT NOT NULL DEFAULT 'NOT_PAID', at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS phoenix_ac_quota (bucket TEXT PRIMARY KEY, n INTEGER NOT NULL, expires_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS phoenix_ac_request (
 client_id TEXT NOT NULL, request_key TEXT NOT NULL, input_hash TEXT NOT NULL,
 receipt_id TEXT NOT NULL, state TEXT NOT NULL, at INTEGER NOT NULL,
 PRIMARY KEY(client_id,request_key));
