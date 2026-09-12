CREATE TABLE IF NOT EXISTS accounts (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  csu_username  TEXT NOT NULL,
  password_enc  TEXT NOT NULL,
  enabled       INTEGER NOT NULL DEFAULT 1,
  window_start  TEXT NOT NULL,
  window_end    TEXT NOT NULL,
  jd            REAL,
  wd            REAL,
  dkdz          TEXT NOT NULL DEFAULT '',
  casual        TEXT,
  token         TEXT,
  cookies       TEXT,
  token_at      TEXT,
  next_run_at   TEXT,
  last_run_at   TEXT,
  last_status   TEXT,
  last_message  TEXT,
  needs_reauth  INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL, auth_error TEXT NOT NULL DEFAULT '',
  UNIQUE(user_id, csu_username)
);
CREATE TABLE IF NOT EXISTS login_codes (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  email       TEXT NOT NULL,
  code_hash   TEXT NOT NULL,
  expires_at  TEXT NOT NULL,
  used        INTEGER NOT NULL DEFAULT 0,
  attempts    INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS records (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  run_at     TEXT NOT NULL,
  trigger    TEXT NOT NULL,
  status     TEXT NOT NULL,
  message    TEXT,
  dksj       TEXT
);
CREATE TABLE IF NOT EXISTS scheduler_lease (
  id           INTEGER PRIMARY KEY CHECK (id = 1),
  owner        TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash  TEXT PRIMARY KEY,
  user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at  TEXT NOT NULL,
  expires_at  TEXT NOT NULL,
  user_agent  TEXT
);
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  email         TEXT NOT NULL UNIQUE,
  created_at    TEXT NOT NULL,
  last_login_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_accounts_auth_error ON accounts(auth_error);
CREATE INDEX IF NOT EXISTS idx_codes_email ON login_codes(email, id DESC);
CREATE INDEX IF NOT EXISTS idx_records_account ON records(account_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_records_account_run ON records(account_id, run_at);
