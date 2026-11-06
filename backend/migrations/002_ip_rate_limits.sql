CREATE TABLE app.ip_rate_limits (
    ip_hash text PRIMARY KEY CHECK (length(ip_hash) = 64),
    second_window timestamptz NOT NULL,
    second_count integer NOT NULL,
    minute_window timestamptz NOT NULL,
    minute_count integer NOT NULL
);
CREATE INDEX ip_rate_limits_expiry_idx ON app.ip_rate_limits (minute_window);
