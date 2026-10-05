# AI-DLC Audit Log

## 2026-10-04 — Resume

- Resumed approved Jev Trading Lab implementation plan in existing isolated worktree `feature/implementation`.
- Current gate: Task 8 append-only journal remains pending independent re-review after commit `4af1cb5`.
- Decision: obtain independent specification, correctness, and security/database reviews before advancing.
- Advancement rule: zero BLOCKER/MAJOR findings, fresh full verification, committed delivery evidence.
- Next unit after Task 8 approval: Task 9 crash-durable blob publication, implemented test-first.

## Task 8 independent review gate

- Result: NOT APPROVED. Three reviewers converged on a supported-boundary callback injection through mutable `row_factory`.
- Additional security finding: public arbitrary migration-package injection can mutate guarded journal rows during later migrations.
- Reliability finding: concurrent append test barrier timeout is load-sensitive.
- Decision: fix all findings test-first, rerun independent reviews, and do not advance to Task 9 until zero BLOCKER/MAJOR.

## Task 8 re-review after `bafb2ae`

- Result: NOT APPROVED. Fresh adversarial review found caller code still executable inside privileged SQLite scopes.
- BLOCKER: public migration `clock` callback executes under migration authorizer.
- BLOCKER/MAJOR: process-global `sqlite3.register_adapter(str, ...)` callback executes while journal INSERT is authorized.
- MAJOR: `SecureConnection.setconfig` remains mutable and can enable writable schema or disable triggers.
- Disputed but actionable hardening: remove arbitrary-package execution helper from production rather than relying only on underscore privacy.
- Decision: redesign privileged windows so they execute no caller-controlled callbacks; seal mutable SQLite configuration; remove generic arbitrary migration execution from shipped runtime.

## Task 8 third re-review after `8bfb961`

- Result: NOT APPROVED. All prior privileged callback/configuration findings closed.
- Remaining BLOCKER: `apply_migrations` accepts raw `sqlite3.Connection` and directly constructed unsealed `SecureConnection`; preinstalled trace/progress/function callbacks then run under migration privilege.
- Decision: public migration boundary must require a factory-created, already sealed `SecureConnection`; reject all raw/unsealed connections before authorizer changes or SQL. Add exhaustive preinstalled-callback regressions.

## Task 8 final review after `c08a7f6`

- Result: NOT APPROVED (spec reviewer passed; quality and adversarial reviewers found supported-boundary gaps).
- MAJOR: caller `sys.settrace` callbacks can execute while migration/journal authorizers are privileged; asynchronous exceptions can strand privilege because installation occurred before the restoration `try/finally`.
- MAJOR: `AppendOnlyJournal` accepts a directly constructed `SecureConnection` subclass that spoofs overrideable seal properties and retains a hostile trace callback.
- Decision: make privileged transitions exception-safe at every instruction boundary, fail closed on active interpreter tracing/profiling for supported operations, and require exact factory provenance for journal construction/write scope.

## Task 8 review after `135d63f` — architectural stop

- Result: NOT APPROVED. One-time trace/profile guards are fundamentally raceable.
- BLOCKER: Python signals execute arbitrary SQL during migration/journal privilege.
- BLOCKER: `threading.settrace_all_threads` can install hooks after preflight; `sys.gettrace` can be monkeypatched.
- MAJOR: parent-descriptor transfer has a BaseException double-close interval.
- Decision: stop narrow callback patches. Eliminate the temporary in-process elevated-authorizer model (or isolate elevation in a callback-free process with a verified protocol). Preserve the same public economic invariants and fix descriptor ownership transfer atomically.

## Task 8 architecture decision

- Selected hybrid replacement rather than weakening the controlled-writer contract.
- Runtime: public `SecureConnection` keeps one permanent restrictive authorizer; each repository append opens a private fixed-role writer connection whose authorizer never changes and permits direct INSERT only into the selected journal table. Public callbacks retain only the public connection, which stays denied before/during/after append.
- Migrations: packaged migrations execute in a fresh isolated interpreter with a fixed migration-role connection; parent callbacks, signals, adapters, and SQLite handles are not inherited. Parent verifies the packaged checksum prefix after child exit.
- Filesystem descriptors: replace raw transfer flag with idempotent shared ownership to eliminate double-close under BaseException.
- Security claim remains controlled API change enforcement, not a sandbox: raw connections, private capability theft/frame inspection, base-descriptor dispatch, same-user filesystem writes, and process-memory compromise remain outside scope.

## Task 8 redesign review after `049ae1b`

- Result: NOT APPROVED.
- BLOCKER: process-wide CPython audit hook can steal a newly-created SQLite handle before Python code installs its fixed authorizer. This is technically the same capability-theft class as frame inspection/private-helper import and cannot be prevented in pure Python. Contract correction: deliberate audit-hook handle capture is explicitly outside the controlled-API boundary; passive audit hooks remain supported.
- BLOCKER/correctness: internal writer did not bind the SQLite-opened file to the validated inode. Fix despite same-user pathname replacement already being outside the threat boundary.
- MAJOR: helper output cap was post-buffering; implement incremental bounded capture.
- MAJOR: malformed/lost successful helper reply must resolve through authoritative database readback.
- MAJOR: `_DirectoryHandle.close` can leak if asynchronously interrupted between logical release and `os.close`; redesign cleanup and preserve original exceptions.
- MAJOR: remove obsolete skipped tests and rewrite all still-valid invariants; final default suite must have no unconditional architecture skips.
- MINOR: canonical helper path validation must reject lexical `..`/non-normalized paths.

## Task 8 final redesign review after `86e739c`

- Result: NOT APPROVED despite 794 green tests.
- BLOCKER: privileged internal writer still failed to bind SQLite's actual opened main descriptor to the validated inode during swap-restore.
- MAJOR: complete authoritative migration readback did not override malformed zero-exit helper reply.
- MAJOR: `_DirectoryHandle.close` retried an ambiguous numeric descriptor after underlying close may have succeeded, enabling double-close after FD reuse.
- MAJOR: helper process group was not killed/reaped on arbitrary BaseException.
- MAJOR: helper output limit was per stream rather than combined stdout+stderr.
- Decision: add exact reproductions first, implement fail-closed fixes, and repeat independent review.

## Task 8 review after `c36a1d8`

- Result: spec PASS, quality/security NOT APPROVED.
- MAJOR: helper closes stdout/stderr then stays alive; selector loop ends and unbounded `wait()` bypasses timeout.
- MAJOR: `_open_private_parent` closes a raw traversal FD before relinquishing ownership; trace interruption can reuse the number and cleanup closes the unrelated FD.
- MINOR: failed cross-thread `SecureConnection.close()` closes retained directory provenance while SQLite connection remains usable.
- Decision: fix exact reproductions test-first; audit every raw-FD ownership transition, not only `_DirectoryHandle`.

## Task 8 boundary correction after `c668b60`

- Reviews found valid raw-FD ownership gaps in parent traversal and successful-close cleanup; these remain actionable.
- Pure Python cannot guarantee cleanup if a malicious `sys.settrace` callback raises at the exact bytecode between a C resource acquisition and assignment, just as it cannot prevent frame inspection/private capability theft. Such targeted interpreter sabotage is now explicitly outside scope; passive hooks and ordinary signal cancellation remain supported.
- The migration helper is fixed, packaged, reviewed code. A modified helper deliberately forking and calling `setsid()` to escape supervision is a malicious trusted-component replacement and outside scope. Ordinary helper hangs, crashes, same-group descendants, closed pipes, output overflow, timeout, and parent cancellation remain protected.
- Action: retain `_DirectoryHandle` across function boundaries (no raw release/return transfer); close provenance only after successful SQLite close; supervise trusted helper through EOF/exit and ordinary cancellation; update claims/tests accordingly.

## Task 8 closure

- Final reviewed HEAD: `52d03d5`.
- Independent specification review: APPROVED, zero BLOCKER/MAJOR.
- Independent quality/correctness review: APPROVED, zero BLOCKER/MAJOR.
- Independent adversarial boundary review: APPROVED, zero BLOCKER/MAJOR.
- Fresh gates: 822 tests passed, zero skips; focused 339; artifact policy 59; lint/format/types/policy/dependency/build/wheel gates passed.
- Task 8 closed. Task 9 may begin.

## Task 9 first independent review

- Result: NOT APPROVED.
- BLOCKER: new root/shard directory entries were not durably fsynced before the reference callback.
- BLOCKER: pathname-based stale-temp traversal could follow a raced shard symlink and delete outside the store.
- BLOCKER: credential/signed-URL redaction missed common Google/Azure/CloudFront and camelCase/full-width structured names; envelope repr leaked originals.
- MAJOR: link/unlink no-replace fallback was not crash-recoverable.
- MAJOR: redacting JSON rewrote unrelated body bytes, violating exact artifact preservation.
- MAJOR: volatile/wrong root filesystem and root mode/path drift were not revalidated.
- MAJOR: FIFO target open could block.
- MAJOR: metadata parsing had no early aggregate bounds.
- Decision: fix every finding test-first with real subprocess/filesystem tests before re-review.

## Task 9 remediation re-review at `8c340d1`

- Result: NOT APPROVED.
- BLOCKER: malformed declared JSON could fail open after an earlier malformed escape and preserve an escaped secret key/value.
- BLOCKER: common OAuth query/header credentials (`client_secret`, `access_token`, `Client-Secret`, `Access-Token`) were not redacted.
- BLOCKER: nested root ancestors were created recursively without fsyncing every new directory entry.
- MAJOR: blob-root path identity relied on `(st_dev, st_ino)` after closing the validated descriptor, allowing immediate inode reuse.
- MAJOR: Unicode encoding errors could expose original secrets in exception text/repr.
- MAJOR: JSON redaction allocated an unused per-character offsets list, causing ~43x memory amplification.
- MAJOR: exact CI pytest command could create source-tree `__pycache__`; root cause required rather than workaround.
- Decision: exact RED reproductions first; pin blob-root descriptor, create/fsync ancestry descriptor-relatively, fail malformed declared JSON closed, broaden structured OAuth redaction, sanitize all validation errors, remove amplification, and fix bytecode-producing test subprocesses.
