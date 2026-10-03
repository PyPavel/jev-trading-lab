CREATE TABLE main.manifests (
    manifest_id TEXT PRIMARY KEY CHECK (
        typeof(manifest_id) = 'text'
        AND length(CAST(manifest_id AS BLOB)) = 64
        AND instr(manifest_id, char(0)) = 0
        AND manifest_id NOT GLOB '*[^0-9a-f]*'
    ),
    canonical_json BLOB NOT NULL CHECK (
        typeof(canonical_json) = 'blob'
        AND length(canonical_json) BETWEEN 1 AND 262144
    ),
    schema_version INTEGER NOT NULL CHECK (
        typeof(schema_version) = 'integer' AND schema_version = 1
    ),
    created_at_us INTEGER NOT NULL CHECK (
        typeof(created_at_us) = 'integer'
        AND created_at_us BETWEEN 0 AND 253402300799999999
    )
) STRICT, WITHOUT ROWID;

CREATE TABLE main.run_windows (
    run_window_id TEXT PRIMARY KEY CHECK (
        typeof(run_window_id) = 'text'
        AND length(CAST(run_window_id AS BLOB)) = 64
        AND instr(run_window_id, char(0)) = 0
        AND run_window_id NOT GLOB '*[^0-9a-f]*'
    ),
    canonical_json BLOB NOT NULL CHECK (
        typeof(canonical_json) = 'blob'
        AND length(canonical_json) BETWEEN 1 AND 262144
    ),
    enrollment_start_us INTEGER NOT NULL CHECK (
        typeof(enrollment_start_us) = 'integer'
        AND enrollment_start_us BETWEEN -62135596800000000 AND 253402300799999999
    ),
    enrollment_end_us INTEGER NOT NULL CHECK (
        typeof(enrollment_end_us) = 'integer'
        AND enrollment_end_us BETWEEN -62135596800000000 AND 253402300799999999
    ),
    maturation_cutoff_us INTEGER NOT NULL CHECK (
        typeof(maturation_cutoff_us) = 'integer'
        AND maturation_cutoff_us BETWEEN -62135596800000000 AND 253402300799999999
    ),
    calendar_name TEXT NOT NULL CHECK (
        calendar_name = 'XNYS' AND instr(calendar_name, char(0)) = 0
    ),
    calendar_timezone TEXT NOT NULL CHECK (
        calendar_timezone = 'America/New_York'
        AND instr(calendar_timezone, char(0)) = 0
    ),
    calendar_version TEXT NOT NULL CHECK (
        typeof(calendar_version) = 'text'
        AND length(calendar_version) BETWEEN 1 AND 255
        AND instr(calendar_version, char(0)) = 0
    ),
    calendar_lock_sha256 TEXT NOT NULL CHECK (
        typeof(calendar_lock_sha256) = 'text'
        AND length(CAST(calendar_lock_sha256 AS BLOB)) = 64
        AND instr(calendar_lock_sha256, char(0)) = 0
        AND calendar_lock_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    calendar_artifact_sha256 TEXT NOT NULL CHECK (
        typeof(calendar_artifact_sha256) = 'text'
        AND length(CAST(calendar_artifact_sha256 AS BLOB)) = 64
        AND instr(calendar_artifact_sha256, char(0)) = 0
        AND calendar_artifact_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    tm_rule_version TEXT NOT NULL CHECK (
        typeof(tm_rule_version) = 'text'
        AND length(tm_rule_version) BETWEEN 1 AND 255
        AND instr(tm_rule_version, char(0)) = 0
    ),
    tm_rule_sha256 TEXT NOT NULL CHECK (
        typeof(tm_rule_sha256) = 'text'
        AND length(CAST(tm_rule_sha256 AS BLOB)) = 64
        AND instr(tm_rule_sha256, char(0)) = 0
        AND tm_rule_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (enrollment_end_us = enrollment_start_us + 7257600000000),
    CHECK (enrollment_start_us < enrollment_end_us),
    CHECK (enrollment_end_us < maturation_cutoff_us)
) STRICT, WITHOUT ROWID;

CREATE TABLE main.runs (
    run_id TEXT PRIMARY KEY CHECK (
        typeof(run_id) = 'text'
        AND length(CAST(run_id AS BLOB)) = 64
        AND instr(run_id, char(0)) = 0
        AND run_id NOT GLOB '*[^0-9a-f]*'
    ),
    manifest_id TEXT NOT NULL REFERENCES manifests(manifest_id) CHECK (
        typeof(manifest_id) = 'text'
        AND length(CAST(manifest_id AS BLOB)) = 64
        AND instr(manifest_id, char(0)) = 0
        AND manifest_id NOT GLOB '*[^0-9a-f]*'
    ),
    run_window_id TEXT NOT NULL REFERENCES run_windows(run_window_id) CHECK (
        typeof(run_window_id) = 'text'
        AND length(CAST(run_window_id AS BLOB)) = 64
        AND instr(run_window_id, char(0)) = 0
        AND run_window_id NOT GLOB '*[^0-9a-f]*'
    ),
    phase TEXT NOT NULL CHECK (phase = 'phase1' AND instr(phase, char(0)) = 0),
    mode TEXT NOT NULL CHECK (mode = 'paper' AND instr(mode, char(0)) = 0),
    dependency_lock_sha256 TEXT NOT NULL CHECK (
        typeof(dependency_lock_sha256) = 'text'
        AND length(CAST(dependency_lock_sha256 AS BLOB)) = 64
        AND instr(dependency_lock_sha256, char(0)) = 0
        AND dependency_lock_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    registered_at_us INTEGER NOT NULL CHECK (
        typeof(registered_at_us) = 'integer'
        AND registered_at_us BETWEEN 0 AND 253402300799999999
    ),
    UNIQUE (manifest_id, run_window_id, dependency_lock_sha256)
) STRICT, WITHOUT ROWID;

CREATE INDEX main.runs_manifest_idx ON runs(manifest_id);
CREATE INDEX main.runs_window_idx ON runs(run_window_id);

CREATE TABLE main.accounts (
    account_id TEXT PRIMARY KEY CHECK (
        typeof(account_id) = 'text'
        AND length(CAST(account_id AS BLOB)) = 64
        AND instr(account_id, char(0)) = 0
        AND account_id NOT GLOB '*[^0-9a-f]*'
    ),
    run_id TEXT NOT NULL REFERENCES runs(run_id) CHECK (
        typeof(run_id) = 'text'
        AND length(CAST(run_id AS BLOB)) = 64
        AND instr(run_id, char(0)) = 0
        AND run_id NOT GLOB '*[^0-9a-f]*'
    ),
    stream TEXT NOT NULL CHECK (
        stream IN ('polymarket', 'stocks') AND instr(stream, char(0)) = 0
    ),
    arm TEXT NOT NULL CHECK (
        instr(arm, char(0)) = 0
        AND (
            (stream = 'polymarket' AND arm IN ('AI', 'DETERMINISTIC', 'CASH'))
            OR
            (stream = 'stocks' AND arm IN (
                'AI', 'DETERMINISTIC', 'CASH', 'EXPOSURE_MATCHED_PASSIVE'
            ))
        )
    ),
    currency TEXT NOT NULL CHECK (currency = 'USD' AND instr(currency, char(0)) = 0),
    initial_capital_microdollars INTEGER NOT NULL CHECK (
        typeof(initial_capital_microdollars) = 'integer'
        AND initial_capital_microdollars = 10000000000
    ),
    UNIQUE (run_id, stream, arm, currency)
) STRICT, WITHOUT ROWID;

CREATE INDEX main.accounts_run_idx ON accounts(run_id);

CREATE TABLE main.observations (
    observation_id TEXT PRIMARY KEY CHECK (
        typeof(observation_id) = 'text'
        AND length(CAST(observation_id AS BLOB)) = 64
        AND instr(observation_id, char(0)) = 0
        AND observation_id NOT GLOB '*[^0-9a-f]*'
    ),
    run_id TEXT REFERENCES runs(run_id) CHECK (
        run_id IS NULL OR (
            typeof(run_id) = 'text'
            AND length(CAST(run_id AS BLOB)) = 64
            AND instr(run_id, char(0)) = 0
            AND run_id NOT GLOB '*[^0-9a-f]*'
        )
    ),
    logical_key TEXT NOT NULL CHECK (
        typeof(logical_key) = 'text'
        AND length(CAST(logical_key AS BLOB)) = 64
        AND instr(logical_key, char(0)) = 0
        AND logical_key NOT GLOB '*[^0-9a-f]*'
    ),
    source_uri TEXT NOT NULL CHECK (
        typeof(source_uri) = 'text'
        AND length(source_uri) BETWEEN 1 AND 4096
        AND instr(source_uri, char(0)) = 0
    ),
    published_at_us INTEGER CHECK (
        published_at_us IS NULL
        OR (
            typeof(published_at_us) = 'integer'
            AND published_at_us BETWEEN -62135596800000000 AND 253402300799999999
        )
    ),
    retrieved_at_us INTEGER NOT NULL CHECK (
        typeof(retrieved_at_us) = 'integer'
        AND retrieved_at_us BETWEEN -62135596800000000 AND 253402300799999999
    ),
    status TEXT NOT NULL CHECK (
        status IN ('ok', 'missing', 'invalid', 'stale')
        AND instr(status, char(0)) = 0
    ),
    content_sha256 TEXT NOT NULL CHECK (
        typeof(content_sha256) = 'text'
        AND length(CAST(content_sha256 AS BLOB)) = 64
        AND instr(content_sha256, char(0)) = 0
        AND content_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    supersedes_id TEXT UNIQUE REFERENCES observations(observation_id) CHECK (
        supersedes_id IS NULL OR (
            typeof(supersedes_id) = 'text'
            AND length(CAST(supersedes_id AS BLOB)) = 64
            AND instr(supersedes_id, char(0)) = 0
            AND supersedes_id NOT GLOB '*[^0-9a-f]*'
        )
    ),
    CHECK (published_at_us IS NULL OR published_at_us <= retrieved_at_us),
    CHECK (supersedes_id IS NULL OR supersedes_id <> observation_id)
) STRICT, WITHOUT ROWID;

CREATE INDEX main.observations_run_idx ON observations(run_id);

CREATE TRIGGER main.observations_validate_supersession
BEFORE INSERT ON observations
WHEN NEW.supersedes_id IS NOT NULL
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM observations AS predecessor
        WHERE predecessor.observation_id = NEW.supersedes_id
    ) THEN RAISE(ABORT, 'supersession:missing-predecessor') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM observations AS predecessor
        WHERE predecessor.observation_id = NEW.supersedes_id
          AND predecessor.logical_key <> NEW.logical_key
    ) THEN RAISE(ABORT, 'supersession:logical-key') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM observations AS predecessor
        WHERE predecessor.observation_id = NEW.supersedes_id
          AND predecessor.run_id IS NOT NEW.run_id
    ) THEN RAISE(ABORT, 'supersession:run') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM observations AS predecessor
        WHERE predecessor.observation_id = NEW.supersedes_id
          AND predecessor.content_sha256 = NEW.content_sha256
    ) THEN RAISE(ABORT, 'supersession:content') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM observations AS predecessor
        WHERE predecessor.observation_id = NEW.supersedes_id
          AND predecessor.retrieved_at_us >= NEW.retrieved_at_us
    ) THEN RAISE(ABORT, 'supersession:retrieval-time') END;
END;

CREATE TABLE main.artifact_refs (
    artifact_id TEXT PRIMARY KEY CHECK (
        typeof(artifact_id) = 'text'
        AND length(CAST(artifact_id AS BLOB)) = 64
        AND instr(artifact_id, char(0)) = 0
        AND artifact_id NOT GLOB '*[^0-9a-f]*'
    ),
    kind TEXT NOT NULL CHECK (
        kind IN (
            'provider_payload', 'snapshot', 'document', 'evidence_manifest',
            'calendar', 'report', 'other'
        )
        AND instr(kind, char(0)) = 0
    ),
    content_sha256 TEXT NOT NULL CHECK (
        typeof(content_sha256) = 'text'
        AND length(CAST(content_sha256 AS BLOB)) = 64
        AND instr(content_sha256, char(0)) = 0
        AND content_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    byte_size INTEGER NOT NULL CHECK (
        typeof(byte_size) = 'integer' AND byte_size BETWEEN 0 AND 9223372036854775807
    ),
    created_at_us INTEGER NOT NULL CHECK (
        typeof(created_at_us) = 'integer'
        AND created_at_us BETWEEN 0 AND 253402300799999999
    ),
    source_observation_id TEXT REFERENCES observations(observation_id) CHECK (
        source_observation_id IS NULL OR (
            typeof(source_observation_id) = 'text'
            AND length(CAST(source_observation_id AS BLOB)) = 64
            AND instr(source_observation_id, char(0)) = 0
            AND source_observation_id NOT GLOB '*[^0-9a-f]*'
        )
    ),
    storage_relpath TEXT NOT NULL UNIQUE CHECK (
        typeof(storage_relpath) = 'text'
        AND length(storage_relpath) BETWEEN 1 AND 4096
        AND instr(storage_relpath, char(0)) = 0
        AND substr(storage_relpath, 1, 1) <> '/'
        AND substr(storage_relpath, -1, 1) <> '/'
        AND instr(storage_relpath, '//') = 0
        AND instr(storage_relpath, '\') = 0
        AND instr('/' || storage_relpath || '/', '/./') = 0
        AND instr('/' || storage_relpath || '/', '/../') = 0
    )
) STRICT, WITHOUT ROWID;

CREATE INDEX main.artifact_refs_observation_idx
ON artifact_refs(source_observation_id);

CREATE TABLE main.opportunities (
    opportunity_id TEXT PRIMARY KEY CHECK (
        typeof(opportunity_id) = 'text'
        AND length(CAST(opportunity_id AS BLOB)) = 64
        AND instr(opportunity_id, char(0)) = 0
        AND opportunity_id NOT GLOB '*[^0-9a-f]*'
    ),
    run_id TEXT NOT NULL REFERENCES runs(run_id) CHECK (
        typeof(run_id) = 'text'
        AND length(CAST(run_id AS BLOB)) = 64
        AND instr(run_id, char(0)) = 0
        AND run_id NOT GLOB '*[^0-9a-f]*'
    ),
    stream TEXT NOT NULL CHECK (
        stream IN ('polymarket', 'stocks') AND instr(stream, char(0)) = 0
    ),
    subject_id TEXT NOT NULL CHECK (
        typeof(subject_id) = 'text'
        AND length(subject_id) BETWEEN 1 AND 255
        AND instr(subject_id, char(0)) = 0
    ),
    scheduled_for_us INTEGER NOT NULL CHECK (
        typeof(scheduled_for_us) = 'integer'
        AND scheduled_for_us BETWEEN -62135596800000000 AND 253402300799999999
    ),
    snapshot_id TEXT REFERENCES artifact_refs(artifact_id) CHECK (
        snapshot_id IS NULL OR (
            typeof(snapshot_id) = 'text'
            AND length(CAST(snapshot_id AS BLOB)) = 64
            AND instr(snapshot_id, char(0)) = 0
            AND snapshot_id NOT GLOB '*[^0-9a-f]*'
        )
    ),
    UNIQUE (run_id, stream, subject_id, scheduled_for_us)
) STRICT, WITHOUT ROWID;

CREATE INDEX main.opportunities_run_idx ON opportunities(run_id);

CREATE TRIGGER main.opportunities_validate_snapshot_kind
BEFORE INSERT ON opportunities
WHEN NEW.snapshot_id IS NOT NULL
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM artifact_refs
        WHERE artifact_id = NEW.snapshot_id AND kind = 'snapshot'
    ) THEN RAISE(ABORT, 'opportunity:snapshot-kind') END;
END;

CREATE TRIGGER main.manifests_immutable_conflict
BEFORE INSERT ON manifests
WHEN EXISTS (
    SELECT 1 FROM manifests AS old WHERE old.manifest_id = NEW.manifest_id
      AND (old.canonical_json IS NOT NEW.canonical_json
           OR old.schema_version IS NOT NEW.schema_version
           OR old.created_at_us IS NOT NEW.created_at_us)
)
BEGIN SELECT RAISE(ABORT, 'immutable-conflict:manifests'); END;
CREATE TRIGGER main.run_windows_immutable_conflict
BEFORE INSERT ON run_windows
WHEN EXISTS (
    SELECT 1 FROM run_windows AS old WHERE old.run_window_id = NEW.run_window_id
      AND (old.canonical_json IS NOT NEW.canonical_json
           OR old.enrollment_start_us IS NOT NEW.enrollment_start_us
           OR old.enrollment_end_us IS NOT NEW.enrollment_end_us
           OR old.maturation_cutoff_us IS NOT NEW.maturation_cutoff_us
           OR old.calendar_name IS NOT NEW.calendar_name
           OR old.calendar_timezone IS NOT NEW.calendar_timezone
           OR old.calendar_version IS NOT NEW.calendar_version
           OR old.calendar_lock_sha256 IS NOT NEW.calendar_lock_sha256
           OR old.calendar_artifact_sha256 IS NOT NEW.calendar_artifact_sha256
           OR old.tm_rule_version IS NOT NEW.tm_rule_version
           OR old.tm_rule_sha256 IS NOT NEW.tm_rule_sha256)
)
BEGIN SELECT RAISE(ABORT, 'immutable-conflict:run_windows'); END;
CREATE TRIGGER main.runs_immutable_conflict
BEFORE INSERT ON runs
WHEN EXISTS (
    SELECT 1 FROM runs AS old WHERE old.run_id = NEW.run_id
      AND (old.manifest_id IS NOT NEW.manifest_id
           OR old.run_window_id IS NOT NEW.run_window_id
           OR old.phase IS NOT NEW.phase
           OR old.mode IS NOT NEW.mode
           OR old.dependency_lock_sha256 IS NOT NEW.dependency_lock_sha256
           OR old.registered_at_us IS NOT NEW.registered_at_us)
)
BEGIN SELECT RAISE(ABORT, 'immutable-conflict:runs'); END;
CREATE TRIGGER main.accounts_immutable_conflict
BEFORE INSERT ON accounts
WHEN EXISTS (
    SELECT 1 FROM accounts AS old WHERE old.account_id = NEW.account_id
      AND (old.run_id IS NOT NEW.run_id
           OR old.stream IS NOT NEW.stream
           OR old.arm IS NOT NEW.arm
           OR old.currency IS NOT NEW.currency
           OR old.initial_capital_microdollars IS NOT NEW.initial_capital_microdollars)
)
BEGIN SELECT RAISE(ABORT, 'immutable-conflict:accounts'); END;
CREATE TRIGGER main.opportunities_immutable_conflict
BEFORE INSERT ON opportunities
WHEN EXISTS (
    SELECT 1 FROM opportunities AS old WHERE old.opportunity_id = NEW.opportunity_id
      AND (old.run_id IS NOT NEW.run_id
           OR old.stream IS NOT NEW.stream
           OR old.subject_id IS NOT NEW.subject_id
           OR old.scheduled_for_us IS NOT NEW.scheduled_for_us
           OR old.snapshot_id IS NOT NEW.snapshot_id)
)
BEGIN SELECT RAISE(ABORT, 'immutable-conflict:opportunities'); END;
CREATE TRIGGER main.observations_immutable_conflict
BEFORE INSERT ON observations
WHEN EXISTS (
    SELECT 1 FROM observations AS old WHERE old.observation_id = NEW.observation_id
      AND (old.run_id IS NOT NEW.run_id
           OR old.logical_key IS NOT NEW.logical_key
           OR old.source_uri IS NOT NEW.source_uri
           OR old.published_at_us IS NOT NEW.published_at_us
           OR old.retrieved_at_us IS NOT NEW.retrieved_at_us
           OR old.status IS NOT NEW.status
           OR old.content_sha256 IS NOT NEW.content_sha256
           OR old.supersedes_id IS NOT NEW.supersedes_id)
)
BEGIN SELECT RAISE(ABORT, 'immutable-conflict:observations'); END;
CREATE TRIGGER main.artifact_refs_immutable_conflict
BEFORE INSERT ON artifact_refs
WHEN EXISTS (
    SELECT 1 FROM artifact_refs AS old WHERE old.artifact_id = NEW.artifact_id
      AND (old.kind IS NOT NEW.kind
           OR old.content_sha256 IS NOT NEW.content_sha256
           OR old.byte_size IS NOT NEW.byte_size
           OR old.created_at_us IS NOT NEW.created_at_us
           OR old.source_observation_id IS NOT NEW.source_observation_id
           OR old.storage_relpath IS NOT NEW.storage_relpath)
)
BEGIN SELECT RAISE(ABORT, 'immutable-conflict:artifact_refs'); END;

CREATE TRIGGER main.manifests_immutable_update
BEFORE UPDATE ON manifests BEGIN SELECT RAISE(ABORT, 'immutable:manifests'); END;
CREATE TRIGGER main.manifests_immutable_delete
BEFORE DELETE ON manifests BEGIN SELECT RAISE(ABORT, 'immutable:manifests'); END;
CREATE TRIGGER main.run_windows_immutable_update
BEFORE UPDATE ON run_windows BEGIN SELECT RAISE(ABORT, 'immutable:run_windows'); END;
CREATE TRIGGER main.run_windows_immutable_delete
BEFORE DELETE ON run_windows BEGIN SELECT RAISE(ABORT, 'immutable:run_windows'); END;
CREATE TRIGGER main.runs_immutable_update
BEFORE UPDATE ON runs BEGIN SELECT RAISE(ABORT, 'immutable:runs'); END;
CREATE TRIGGER main.runs_immutable_delete
BEFORE DELETE ON runs BEGIN SELECT RAISE(ABORT, 'immutable:runs'); END;
CREATE TRIGGER main.accounts_immutable_update
BEFORE UPDATE ON accounts BEGIN SELECT RAISE(ABORT, 'immutable:accounts'); END;
CREATE TRIGGER main.accounts_immutable_delete
BEFORE DELETE ON accounts BEGIN SELECT RAISE(ABORT, 'immutable:accounts'); END;
CREATE TRIGGER main.opportunities_immutable_update
BEFORE UPDATE ON opportunities BEGIN SELECT RAISE(ABORT, 'immutable:opportunities'); END;
CREATE TRIGGER main.opportunities_immutable_delete
BEFORE DELETE ON opportunities BEGIN SELECT RAISE(ABORT, 'immutable:opportunities'); END;
CREATE TRIGGER main.observations_immutable_update
BEFORE UPDATE ON observations BEGIN SELECT RAISE(ABORT, 'immutable:observations'); END;
CREATE TRIGGER main.observations_immutable_delete
BEFORE DELETE ON observations BEGIN SELECT RAISE(ABORT, 'immutable:observations'); END;
CREATE TRIGGER main.artifact_refs_immutable_update
BEFORE UPDATE ON artifact_refs BEGIN SELECT RAISE(ABORT, 'immutable:artifact_refs'); END;
CREATE TRIGGER main.artifact_refs_immutable_delete
BEFORE DELETE ON artifact_refs BEGIN SELECT RAISE(ABORT, 'immutable:artifact_refs'); END;
