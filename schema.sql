CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT PRIMARY KEY,
    gender VARCHAR(10),
    age INT,
    interests TEXT,
    status VARCHAR(20) DEFAULT 'idle' -- idle, waiting, chatting
);

CREATE TABLE IF NOT EXISTS queue (
    user_id BIGINT PRIMARY KEY,
    joined_at TIMESTAMP DEFAULT NOW()
);

-- двухсторонние пары (быстрый поиск партнёра по user_id)
CREATE TABLE IF NOT EXISTS pairs (
    user_id BIGINT PRIMARY KEY,
    partner_id BIGINT NOT NULL,
    started_at TIMESTAMP DEFAULT NOW()
);
