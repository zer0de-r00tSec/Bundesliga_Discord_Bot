-- Optional: Tabellen legt der Bot beim Start selbst an (init_db).
-- mysql -u football_bot -p football_bot < app/migrations/001_initial.sql

CREATE TABLE IF NOT EXISTS leagues (
    id   VARCHAR(10)  NOT NULL,
    name VARCHAR(100) NOT NULL,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT IGNORE INTO leagues (id, name) VALUES
    ('bl1', '1. Bundesliga'), ('bl2', '2. Bundesliga'), ('bl3', '3. Liga'), ('dfb', 'DFB-Pokal');

CREATE TABLE IF NOT EXISTS teams (
    id         INT          NOT NULL,
    name       VARCHAR(150) NOT NULL,
    short_name VARCHAR(100),
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Aliase werden nach dem ersten Sync automatisch gesetzt (app/sync.py: seed_aliases)
CREATE TABLE IF NOT EXISTS team_aliases (
    alias   VARCHAR(100) NOT NULL,
    team_id INT          NOT NULL,
    PRIMARY KEY (alias),
    FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS matches (
    match_id       INT         NOT NULL,
    league_id      VARCHAR(10) NOT NULL,
    season         INT         NOT NULL,
    matchday       INT         NOT NULL,
    group_name     VARCHAR(100),
    kickoff_utc    DATETIME    NOT NULL,
    kickoff_local  DATETIME,
    home_team_id   INT         NOT NULL,
    away_team_id   INT         NOT NULL,
    home_score     INT,
    away_score     INT,
    is_finished    BOOLEAN     NOT NULL DEFAULT FALSE,
    status         VARCHAR(50),
    last_update    DATETIME,
    last_synced_at DATETIME,
    raw_json       JSON,
    PRIMARY KEY (match_id),
    FOREIGN KEY (league_id)    REFERENCES leagues(id),
    FOREIGN KEY (home_team_id) REFERENCES teams(id),
    FOREIGN KEY (away_team_id) REFERENCES teams(id),
    INDEX idx_kickoff  (kickoff_utc),
    INDEX idx_matchday (league_id, season, matchday)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS match_events (
    event_key   VARCHAR(255) NOT NULL,
    match_id    INT          NOT NULL,
    event_type  VARCHAR(50)  NOT NULL,
    minute      INT,
    home_score  INT,
    away_score  INT,
    player_name VARCHAR(150),
    team_id     INT,
    raw_json    JSON,
    created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (event_key),
    FOREIGN KEY (match_id) REFERENCES matches(match_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Spaltennamen telegram_* sind historisch; im Code: channel_id / message_id
CREATE TABLE IF NOT EXISTS posted_events (
    event_key           VARCHAR(255) NOT NULL,
    match_id            INT          NOT NULL,
    telegram_chat_id    BIGINT       NOT NULL,
    telegram_message_id BIGINT,
    posted_at           DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (event_key),
    INDEX idx_match (match_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS api_cache (
    cache_key  VARCHAR(255) NOT NULL,
    payload    JSON,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (cache_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS bot_state (
    state_key   VARCHAR(100) NOT NULL,
    state_value TEXT,
    updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (state_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
