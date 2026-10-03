CREATE TABLE main.manifests (
    manifest_id TEXT PRIMARY KEY CHECK (
        length(manifest_id) = 64
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
        length(run_window_id) = 64
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
    calendar_name TEXT NOT NULL CHECK (calendar_name = 'XNYS'),
    calendar_timezone TEXT NOT NULL CHECK (calendar_timezone = 'America/New_York'),
    calendar_version TEXT NOT NULL CHECK (
        typeof(calendar_version) = 'text' AND length(calendar_version) BETWEEN 1 AND 255
    ),
    calendar_lock_sha256 TEXT NOT NULL CHECK (
        length(calendar_lock_sha256) = 64
        AND calendar_lock_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    calendar_artifact_sha256 TEXT NOT NULL CHECK (
        length(calendar_artifact_sha256) = 64
        AND calendar_artifact_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    tm_rule_version TEXT NOT NULL CHECK (
        typeof(tm_rule_version) = 'text' AND length(tm_rule_version) BETWEEN 1 AND 255
    ),
    tm_rule_sha256 TEXT NOT NULL CHECK (
        length(tm_rule_sha256) = 64
        AND tm_rule_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (enrollment_end_us = enrollment_start_us + 7257600000000),
    CHECK (enrollment_start_us < enrollment_end_us),
    CHECK (enrollment_end_us < maturation_cutoff_us)
) STRICT, WITHOUT ROWID;

CREATE TABLE main.runs (
    run_id TEXT PRIMARY KEY CHECK (
        length(run_id) = 64
        AND run_id NOT GLOB '*[^0-9a-f]*'
    ),
    manifest_id TEXT NOT NULL REFERENCES manifests(manifest_id),
    run_window_id TEXT NOT NULL REFERENCES run_windows(run_window_id),
    phase TEXT NOT NULL CHECK (phase = 'phase1'),
    mode TEXT NOT NULL CHECK (mode = 'paper'),
    dependency_lock_sha256 TEXT NOT NULL CHECK (
        length(dependency_lock_sha256) = 64
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
        length(account_id) = 64
        AND account_id NOT GLOB '*[^0-9a-f]*'
    ),
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    stream TEXT NOT NULL CHECK (stream IN ('polymarket', 'stocks')),
    arm TEXT NOT NULL CHECK (
        (stream = 'polymarket' AND arm IN ('AI', 'DETERMINISTIC', 'CASH'))
        OR
        (stream = 'stocks' AND arm IN (
            'AI', 'DETERMINISTIC', 'CASH', 'EXPOSURE_MATCHED_PASSIVE'
        ))
    ),
    currency TEXT NOT NULL CHECK (currency = 'USD'),
    initial_capital_microdollars INTEGER NOT NULL CHECK (
        typeof(initial_capital_microdollars) = 'integer'
        AND initial_capital_microdollars BETWEEN 1 AND 9223372036854775807
    ),
    UNIQUE (run_id, stream, arm, currency)
) STRICT, WITHOUT ROWID;

CREATE INDEX main.accounts_run_idx ON accounts(run_id);

CREATE TABLE main.observations (
    observation_id TEXT PRIMARY KEY CHECK (
        length(observation_id) = 64
        AND observation_id NOT GLOB '*[^0-9a-f]*'
    ),
    run_id TEXT REFERENCES runs(run_id),
    logical_key TEXT NOT NULL CHECK (
        length(logical_key) = 64
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
    status TEXT NOT NULL CHECK (status IN ('ok', 'missing', 'invalid', 'stale')),
    content_sha256 TEXT NOT NULL CHECK (
        length(content_sha256) = 64
        AND content_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    supersedes_id TEXT UNIQUE REFERENCES observations(observation_id),
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
        length(artifact_id) = 64
        AND artifact_id NOT GLOB '*[^0-9a-f]*'
    ),
    kind TEXT NOT NULL CHECK (kind IN (
        'provider_payload', 'snapshot', 'document', 'evidence_manifest',
        'calendar', 'report', 'other'
    )),
    content_sha256 TEXT NOT NULL CHECK (
        length(content_sha256) = 64
        AND content_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    byte_size INTEGER NOT NULL CHECK (
        typeof(byte_size) = 'integer' AND byte_size BETWEEN 0 AND 9223372036854775807
    ),
    created_at_us INTEGER NOT NULL CHECK (
        typeof(created_at_us) = 'integer'
        AND created_at_us BETWEEN 0 AND 253402300799999999
    ),
    source_observation_id TEXT REFERENCES observations(observation_id),
    storage_relpath TEXT NOT NULL CHECK (
        typeof(storage_relpath) = 'text'
        AND length(storage_relpath) BETWEEN 1 AND 4096
        AND instr(storage_relpath, char(0)) = 0
        AND substr(storage_relpath, 1, 1) <> '/'
        AND instr(storage_relpath, '\') = 0
        AND storage_relpath NOT GLOB '../*'
        AND storage_relpath NOT GLOB '*/../*'
        AND storage_relpath NOT GLOB '*/..'
        AND storage_relpath NOT GLOB './*'
        AND storage_relpath NOT GLOB '*/./*'
        AND storage_relpath NOT GLOB '*/.'
    ),
    UNIQUE (content_sha256, storage_relpath)
) STRICT, WITHOUT ROWID;

CREATE INDEX main.artifact_refs_observation_idx
ON artifact_refs(source_observation_id);

CREATE TABLE main.opportunities (
    opportunity_id TEXT PRIMARY KEY CHECK (
        length(opportunity_id) = 64
        AND opportunity_id NOT GLOB '*[^0-9a-f]*'
    ),
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    stream TEXT NOT NULL CHECK (stream IN ('polymarket', 'stocks')),
    subject_id TEXT NOT NULL CHECK (
        typeof(subject_id) = 'text'
        AND length(subject_id) BETWEEN 1 AND 255
        AND instr(subject_id, char(0)) = 0
    ),
    scheduled_for_us INTEGER NOT NULL CHECK (
        typeof(scheduled_for_us) = 'integer'
        AND scheduled_for_us BETWEEN -62135596800000000 AND 253402300799999999
    ),
    eligibility_episode INTEGER NOT NULL CHECK (
        typeof(eligibility_episode) = 'integer'
        AND eligibility_episode BETWEEN 0 AND 9223372036854775807
    ),
    snapshot_id TEXT REFERENCES artifact_refs(artifact_id),
    UNIQUE (run_id, stream, subject_id, eligibility_episode, scheduled_for_us)
) STRICT, WITHOUT ROWID;

CREATE INDEX main.opportunities_run_idx ON opportunities(run_id);

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
