CREATE TABLE main.schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    name TEXT NOT NULL CHECK (
        typeof(name) = 'text'
        AND length(name) BETWEEN 9 AND 255
        AND name NOT GLOB '*[^0-9a-z_.]*'
        AND substr(name, 4, 1) = '_'
        AND substr(name, 5, 1) GLOB '[a-z]'
        AND substr(name, -4) = '.sql'
        AND length(substr(name, 5, length(name) - 8)) > 0
        AND substr(name, 5, length(name) - 8) NOT GLOB '*[^0-9a-z_]*'
        AND substr(name, 1, 3) = printf('%03d', version)
    ),
    sha256 TEXT NOT NULL CHECK (
        length(sha256) = 64
        AND sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    applied_at_us INTEGER NOT NULL CHECK (
        typeof(applied_at_us) = 'integer'
        AND applied_at_us BETWEEN 0 AND 253402300799999999
    )
) STRICT, WITHOUT ROWID;
