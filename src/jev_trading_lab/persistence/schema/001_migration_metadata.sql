CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    name TEXT NOT NULL CHECK (
        name GLOB '[0-9][0-9][0-9]_[a-z0-9_]*.sql'
        AND length(name) >= 9
    ),
    sha256 TEXT NOT NULL CHECK (
        length(sha256) = 64
        AND sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    applied_at_us INTEGER NOT NULL
) STRICT, WITHOUT ROWID;
