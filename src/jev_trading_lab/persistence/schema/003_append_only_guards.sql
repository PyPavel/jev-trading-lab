CREATE TABLE main.journal_entities (
    entity_id TEXT PRIMARY KEY CHECK (
        typeof(entity_id) = 'text'
        AND length(CAST(entity_id AS BLOB)) = 64
        AND instr(entity_id, char(0)) = 0
        AND entity_id NOT GLOB '*[^0-9a-f]*'
    ),
    run_id TEXT NOT NULL REFERENCES runs(run_id) CHECK (
        typeof(run_id) = 'text'
        AND length(CAST(run_id AS BLOB)) = 64
        AND instr(run_id, char(0)) = 0
        AND run_id NOT GLOB '*[^0-9a-f]*'
    ),
    entity_type TEXT NOT NULL CHECK (
        typeof(entity_type) = 'text'
        AND length(CAST(entity_type AS BLOB)) BETWEEN 1 AND 64
        AND instr(entity_type, char(0)) = 0
        AND substr(entity_type, 1, 1) GLOB '[a-z]'
        AND substr(entity_type, -1, 1) GLOB '[a-z0-9]'
        AND entity_type NOT GLOB '*[^a-z0-9_]*'
        AND instr(entity_type, '__') = 0
    ),
    logical_identity TEXT NOT NULL CHECK (
        typeof(logical_identity) = 'text'
        AND length(CAST(logical_identity AS BLOB)) = 64
        AND instr(logical_identity, char(0)) = 0
        AND logical_identity NOT GLOB '*[^0-9a-f]*'
    ),
    created_at_us INTEGER NOT NULL CHECK (
        typeof(created_at_us) = 'integer'
        AND created_at_us BETWEEN 0 AND 253402300799999999
    ),
    payload_sha256 TEXT NOT NULL CHECK (
        typeof(payload_sha256) = 'text'
        AND length(CAST(payload_sha256 AS BLOB)) = 64
        AND instr(payload_sha256, char(0)) = 0
        AND payload_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    canonical_payload BLOB NOT NULL CHECK (
        typeof(canonical_payload) = 'blob'
        AND length(canonical_payload) BETWEEN 1 AND 262144
    ),
    UNIQUE (run_id, entity_type, logical_identity)
) STRICT, WITHOUT ROWID;

CREATE INDEX main.journal_entities_run_idx ON journal_entities(run_id);

CREATE TABLE main.journal_events (
    event_id TEXT PRIMARY KEY CHECK (
        typeof(event_id) = 'text'
        AND length(CAST(event_id AS BLOB)) = 64
        AND instr(event_id, char(0)) = 0
        AND event_id NOT GLOB '*[^0-9a-f]*'
    ),
    entity_id TEXT NOT NULL REFERENCES journal_entities(entity_id) CHECK (
        typeof(entity_id) = 'text'
        AND length(CAST(entity_id AS BLOB)) = 64
        AND instr(entity_id, char(0)) = 0
        AND entity_id NOT GLOB '*[^0-9a-f]*'
    ),
    event_type TEXT NOT NULL CHECK (
        typeof(event_type) = 'text'
        AND length(CAST(event_type AS BLOB)) BETWEEN 1 AND 64
        AND instr(event_type, char(0)) = 0
        AND substr(event_type, 1, 1) GLOB '[a-z]'
        AND substr(event_type, -1, 1) GLOB '[a-z0-9]'
        AND event_type NOT GLOB '*[^a-z0-9_]*'
        AND instr(event_type, '__') = 0
    ),
    sequence INTEGER NOT NULL CHECK (
        typeof(sequence) = 'integer'
        AND sequence BETWEEN 0 AND 9223372036854775807
    ),
    occurred_at_us INTEGER NOT NULL CHECK (
        typeof(occurred_at_us) = 'integer'
        AND occurred_at_us BETWEEN -62135596800000000 AND 253402300799999999
    ),
    payload_sha256 TEXT NOT NULL CHECK (
        typeof(payload_sha256) = 'text'
        AND length(CAST(payload_sha256 AS BLOB)) = 64
        AND instr(payload_sha256, char(0)) = 0
        AND payload_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    canonical_payload BLOB NOT NULL CHECK (
        typeof(canonical_payload) = 'blob'
        AND length(canonical_payload) BETWEEN 1 AND 262144
    ),
    supersedes_event_id TEXT REFERENCES journal_events(event_id) CHECK (
        supersedes_event_id IS NULL OR (
            typeof(supersedes_event_id) = 'text'
            AND length(CAST(supersedes_event_id AS BLOB)) = 64
            AND instr(supersedes_event_id, char(0)) = 0
            AND supersedes_event_id NOT GLOB '*[^0-9a-f]*'
            AND supersedes_event_id <> event_id
        )
    ),
    UNIQUE (entity_id, sequence),
    UNIQUE (supersedes_event_id)
) STRICT, WITHOUT ROWID;

CREATE INDEX main.journal_events_entity_idx ON journal_events(entity_id);

CREATE TRIGGER main.journal_events_validate_supersession
BEFORE INSERT ON journal_events
WHEN NEW.supersedes_event_id IS NOT NULL
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM journal_events AS predecessor
        WHERE predecessor.event_id = NEW.supersedes_event_id
    ) THEN RAISE(ABORT, 'supersession:missing-predecessor') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM journal_events AS predecessor
        WHERE predecessor.event_id = NEW.supersedes_event_id
          AND predecessor.entity_id <> NEW.entity_id
    ) THEN RAISE(ABORT, 'supersession:entity') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM journal_events AS predecessor
        WHERE predecessor.event_id = NEW.supersedes_event_id
          AND predecessor.sequence >= NEW.sequence
    ) THEN RAISE(ABORT, 'supersession:sequence') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM journal_events AS predecessor
        WHERE predecessor.event_id = NEW.supersedes_event_id
          AND predecessor.occurred_at_us >= NEW.occurred_at_us
    ) THEN RAISE(ABORT, 'supersession:time') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM journal_events AS predecessor
        WHERE predecessor.event_id = NEW.supersedes_event_id
          AND (
              predecessor.payload_sha256 = NEW.payload_sha256
              OR predecessor.canonical_payload = NEW.canonical_payload
          )
    ) THEN RAISE(ABORT, 'supersession:payload') END;
END;

CREATE TRIGGER main.journal_entities_immutable_conflict
BEFORE INSERT ON journal_entities
WHEN EXISTS (
    SELECT 1 FROM journal_entities AS old WHERE (
        old.entity_id = NEW.entity_id
        AND (old.run_id IS NOT NEW.run_id
             OR old.entity_type IS NOT NEW.entity_type
             OR old.logical_identity IS NOT NEW.logical_identity
             OR old.created_at_us IS NOT NEW.created_at_us
             OR old.payload_sha256 IS NOT NEW.payload_sha256
             OR old.canonical_payload IS NOT NEW.canonical_payload)
    ) OR (
        old.run_id IS NEW.run_id
        AND old.entity_type IS NEW.entity_type
        AND old.logical_identity IS NEW.logical_identity
        AND old.entity_id IS NOT NEW.entity_id
    )
)
BEGIN SELECT RAISE(ABORT, 'immutable-conflict:journal_entities'); END;

CREATE TRIGGER main.journal_events_immutable_conflict
BEFORE INSERT ON journal_events
WHEN EXISTS (
    SELECT 1 FROM journal_events AS old WHERE (
        old.event_id = NEW.event_id
        AND (old.entity_id IS NOT NEW.entity_id
             OR old.event_type IS NOT NEW.event_type
             OR old.sequence IS NOT NEW.sequence
             OR old.occurred_at_us IS NOT NEW.occurred_at_us
             OR old.payload_sha256 IS NOT NEW.payload_sha256
             OR old.canonical_payload IS NOT NEW.canonical_payload
             OR old.supersedes_event_id IS NOT NEW.supersedes_event_id)
    ) OR (
        old.entity_id IS NEW.entity_id
        AND old.sequence IS NEW.sequence
        AND old.event_id IS NOT NEW.event_id
    ) OR (
        NEW.supersedes_event_id IS NOT NULL
        AND old.supersedes_event_id IS NEW.supersedes_event_id
        AND old.event_id IS NOT NEW.event_id
    )
)
BEGIN SELECT RAISE(ABORT, 'immutable-conflict:journal_events'); END;

CREATE TRIGGER main.journal_entities_immutable_update
BEFORE UPDATE ON journal_entities
BEGIN SELECT RAISE(ABORT, 'immutable:journal_entities'); END;
CREATE TRIGGER main.journal_entities_immutable_delete
BEFORE DELETE ON journal_entities
BEGIN SELECT RAISE(ABORT, 'immutable:journal_entities'); END;
CREATE TRIGGER main.journal_events_immutable_update
BEFORE UPDATE ON journal_events
BEGIN SELECT RAISE(ABORT, 'immutable:journal_events'); END;
CREATE TRIGGER main.journal_events_immutable_delete
BEFORE DELETE ON journal_events
BEGIN SELECT RAISE(ABORT, 'immutable:journal_events'); END;
