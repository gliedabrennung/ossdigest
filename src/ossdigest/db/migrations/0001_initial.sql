PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS repos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    host            TEXT NOT NULL,
    remote_id       TEXT NOT NULL,
    full_name       TEXT NOT NULL,
    norm_name       TEXT NOT NULL,
    url             TEXT NOT NULL,
    description     TEXT,
    language        TEXT,
    license_spdx    TEXT,
    topics          TEXT,
    stars           INTEGER,
    forks           INTEGER,
    open_issues     INTEGER,
    created_at      TEXT,
    pushed_at       TEXT,
    is_archived     INTEGER DEFAULT 0,
    is_fork         INTEGER DEFAULT 0,
    owner_type      TEXT,
    first_seen_at   TEXT NOT NULL,
    last_checked_at TEXT,
    tracked         INTEGER DEFAULT 1,
    UNIQUE (host, remote_id)
);
CREATE INDEX IF NOT EXISTS idx_repos_norm ON repos (norm_name);
CREATE INDEX IF NOT EXISTS idx_repos_tracked ON repos (tracked, last_checked_at);

CREATE TABLE IF NOT EXISTS star_snapshots (
    repo_id     INTEGER NOT NULL REFERENCES repos(id),
    snap_date   TEXT NOT NULL,
    stars       INTEGER NOT NULL,
    PRIMARY KEY (repo_id, snap_date)
);

CREATE TABLE IF NOT EXISTS candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id         INTEGER NOT NULL REFERENCES repos(id),
    source          TEXT NOT NULL,
    source_meta     TEXT,
    discovered_at   TEXT NOT NULL,
    heuristics_ok   INTEGER,
    reject_reason   TEXT,
    readme_hash     TEXT,
    readme_chars    INTEGER,
    stars_delta_7d  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_cand_repo ON candidates (repo_id);
CREATE INDEX IF NOT EXISTS idx_cand_date ON candidates (discovered_at);

CREATE TABLE IF NOT EXISTS judgements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id    INTEGER NOT NULL REFERENCES candidates(id),
    repo_id         INTEGER NOT NULL REFERENCES repos(id),
    readme_hash     TEXT,
    model           TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    raw_response    TEXT NOT NULL,
    is_usable_tool  INTEGER,
    category        TEXT,
    usefulness      INTEGER,
    novelty         INTEGER,
    maturity        INTEGER,
    verdict         TEXT,
    final_score     REAL,
    red_flags       TEXT,
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    cost_usd        REAL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_judge_hash ON judgements (repo_id, readme_hash, prompt_version);

CREATE TABLE IF NOT EXISTS posts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id           INTEGER NOT NULL REFERENCES repos(id),
    judgement_id      INTEGER REFERENCES judgements(id),
    body_html         TEXT NOT NULL,
    status            TEXT NOT NULL,
    priority          REAL DEFAULT 0,
    needs_attention   INTEGER DEFAULT 0,
    writer_model      TEXT,
    writer_prompt_ver TEXT,
    cost_usd          REAL,
    created_at        TEXT NOT NULL,
    moderated_at      TEXT,
    moderator_action  TEXT,
    scheduled_at      TEXT,
    published_at      TEXT,
    tg_message_id     INTEGER,
    error_count       INTEGER DEFAULT 0,
    last_error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_status ON posts (status, priority DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_repo_published
    ON posts (repo_id) WHERE status = 'published';

CREATE TABLE IF NOT EXISTS publish_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id     INTEGER REFERENCES posts(id),
    ts          TEXT NOT NULL,
    action      TEXT NOT NULL,
    detail      TEXT
);

CREATE TABLE IF NOT EXISTS state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blocklist (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    value       TEXT NOT NULL,
    reason      TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    candidates_total    INTEGER DEFAULT 0,
    candidates_new      INTEGER DEFAULT 0,
    heuristics_passed   INTEGER DEFAULT 0,
    judged              INTEGER DEFAULT 0,
    judged_reused       INTEGER DEFAULT 0,
    verdict_publish     INTEGER DEFAULT 0,
    verdict_hold        INTEGER DEFAULT 0,
    verdict_reject      INTEGER DEFAULT 0,
    drafts_created      INTEGER DEFAULT 0,
    cost_usd            REAL DEFAULT 0,
    error               TEXT
);
