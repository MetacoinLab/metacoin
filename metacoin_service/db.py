"""SQLite persistence with explicit migrations, WAL, foreign keys, busy handling and
BEGIN IMMEDIATE transactions. One connection per transaction; never shared across threads."""
from contextlib import contextmanager
import os
import sqlite3
import stat
import time

MIGRATIONS = [
    ('001_initial', """
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE principals (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL CHECK (role IN ('owner','worker','reviewer','viewer')),
        workspace TEXT NOT NULL, created_at INTEGER NOT NULL, revoked_at INTEGER);
    CREATE INDEX principals_ws ON principals(workspace);
    CREATE TABLE credentials (
        id TEXT PRIMARY KEY, principal_id TEXT NOT NULL REFERENCES principals(id),
        kind TEXT NOT NULL CHECK (kind IN ('api','session')), secret_hash TEXT UNIQUE NOT NULL,
        scope TEXT, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, revoked_at INTEGER);
    CREATE INDEX credentials_principal ON credentials(principal_id);
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY, principal_id TEXT NOT NULL REFERENCES principals(id), csrf TEXT NOT NULL,
        created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, revoked_at INTEGER);
    CREATE TABLE contracts (
        id TEXT PRIMARY KEY, workspace TEXT NOT NULL, owner_id TEXT NOT NULL REFERENCES principals(id),
        kind TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN ('draft','frozen')),
        version INTEGER NOT NULL DEFAULT 1, lineage_id TEXT NOT NULL, previous_id TEXT REFERENCES contracts(id),
        title TEXT NOT NULL, policy_json TEXT NOT NULL, params_json TEXT NOT NULL,
        input_artifact_id TEXT, contract_json TEXT, contract_digest TEXT UNIQUE, input_root TEXT,
        reviewer_id TEXT REFERENCES principals(id), expires_at INTEGER, created_at INTEGER NOT NULL, frozen_at INTEGER);
    CREATE INDEX contracts_ws ON contracts(workspace, created_at);
    CREATE TABLE jobs (
        id TEXT PRIMARY KEY, workspace TEXT NOT NULL, contract_id TEXT NOT NULL REFERENCES contracts(id),
        kind TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN ('queued','running','succeeded','failed','cancelled')),
        attempt INTEGER NOT NULL DEFAULT 0, lease_owner TEXT, lease_expires INTEGER, lease_generation INTEGER NOT NULL DEFAULT 0,
        retries_left INTEGER NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0,
        evidence_artifact_id TEXT, evidence_root TEXT, outcome TEXT, summary_json TEXT, error_code TEXT,
        review_state TEXT NOT NULL DEFAULT 'none' CHECK (review_state IN ('none','requested','accepted','rejected')),
        submitted_by TEXT NOT NULL REFERENCES principals(id), created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
        finished_at INTEGER);
    CREATE INDEX jobs_ws ON jobs(workspace, created_at);
    CREATE INDEX jobs_queue ON jobs(state, created_at);
    CREATE TABLE attempts (
        id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), generation INTEGER NOT NULL,
        worker_id TEXT NOT NULL, started_at INTEGER NOT NULL, finished_at INTEGER, outcome TEXT);
    CREATE TABLE artifacts (
        id TEXT PRIMARY KEY, workspace TEXT NOT NULL, kind TEXT NOT NULL, contract_id TEXT REFERENCES contracts(id),
        job_id TEXT REFERENCES jobs(id), owner_id TEXT NOT NULL REFERENCES principals(id),
        storage_name TEXT UNIQUE, encrypted INTEGER NOT NULL, format_version TEXT NOT NULL,
        sha256_ciphertext TEXT, sha256_plaintext TEXT NOT NULL, size_plaintext INTEGER NOT NULL,
        recipients_json TEXT NOT NULL, intended_use TEXT NOT NULL, public INTEGER NOT NULL DEFAULT 0,
        retention_deadline INTEGER, deleted_at INTEGER, created_at INTEGER NOT NULL);
    CREATE INDEX artifacts_job ON artifacts(job_id);
    CREATE TABLE reviewer_keys (
        key_id TEXT PRIMARY KEY, principal_id TEXT NOT NULL REFERENCES principals(id), public_key_hex TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('active','rotated','revoked')), created_at INTEGER NOT NULL,
        status_changed_at INTEGER);
    CREATE TABLE reviews (
        id TEXT PRIMARY KEY, workspace TEXT NOT NULL, job_id TEXT NOT NULL REFERENCES jobs(id),
        reviewer_id TEXT NOT NULL REFERENCES principals(id), key_id TEXT NOT NULL REFERENCES reviewer_keys(key_id),
        decision TEXT NOT NULL CHECK (decision IN ('accepted','rejected')), envelope_json TEXT NOT NULL,
        envelope_digest TEXT UNIQUE NOT NULL, signature_hex TEXT NOT NULL, public_bundle_artifact_id TEXT,
        created_at INTEGER NOT NULL);
    CREATE TABLE events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, workspace TEXT NOT NULL, ts INTEGER NOT NULL, actor_id TEXT NOT NULL,
        event_type TEXT NOT NULL, category TEXT NOT NULL CHECK (category IN ('scientific','administrative','economic')),
        object_type TEXT NOT NULL, object_id TEXT NOT NULL, ref_json TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
    CREATE INDEX events_ws ON events(workspace, seq);
    CREATE TABLE idempotency (
        principal_id TEXT NOT NULL, operation TEXT NOT NULL, key TEXT NOT NULL, request_digest TEXT NOT NULL,
        status INTEGER NOT NULL, response_json TEXT NOT NULL, created_at INTEGER NOT NULL,
        PRIMARY KEY (principal_id, operation, key));
    CREATE TABLE campaigns (
        workspace TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, cap INTEGER NOT NULL, asset TEXT NOT NULL,
        network TEXT NOT NULL, unit TEXT NOT NULL);
    CREATE TABLE payment_actions (
        request_id TEXT PRIMARY KEY, workspace TEXT NOT NULL, job_id TEXT NOT NULL REFERENCES jobs(id),
        provider_mode TEXT NOT NULL, request_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at INTEGER NOT NULL);
    CREATE TABLE sales (
        payment_id TEXT PRIMARY KEY, workspace TEXT NOT NULL, job_id TEXT NOT NULL REFERENCES jobs(id),
        resource TEXT NOT NULL, amount TEXT NOT NULL, asset TEXT NOT NULL, network TEXT NOT NULL, pay_to TEXT NOT NULL,
        provider_mode TEXT NOT NULL, state TEXT NOT NULL, transaction_ref TEXT, payer TEXT, requirements_digest TEXT NOT NULL,
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
    """),
    ('002_batches_templates', """
    CREATE TABLE batches (
        id TEXT PRIMARY KEY, workspace TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES principals(id),
        size INTEGER NOT NULL, total_amount INTEGER NOT NULL, created_at INTEGER NOT NULL);
    ALTER TABLE jobs ADD COLUMN batch_id TEXT REFERENCES batches(id);
    CREATE INDEX jobs_batch ON jobs(batch_id);
    CREATE TABLE templates (
        id TEXT PRIMARY KEY, workspace TEXT NOT NULL, owner_id TEXT NOT NULL REFERENCES principals(id),
        name TEXT NOT NULL, kind TEXT NOT NULL, policy_json TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
    ALTER TABLE contracts ADD COLUMN template_id TEXT REFERENCES templates(id);
    """),
    ('003_datasets_lineage', """
    CREATE TABLE datasets (
        id TEXT PRIMARY KEY, workspace TEXT NOT NULL, owner_id TEXT NOT NULL REFERENCES principals(id),
        name TEXT NOT NULL, kind TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '[]', created_at INTEGER NOT NULL, retired_at INTEGER);
    CREATE INDEX datasets_ws ON datasets(workspace, created_at);
    CREATE TABLE dataset_versions (
        id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES datasets(id), version INTEGER NOT NULL,
        raw_artifact_id TEXT REFERENCES artifacts(id), normalized_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
        raw_sha256 TEXT NOT NULL, normalized_commitment TEXT NOT NULL, normalization_id TEXT NOT NULL,
        row_count INTEGER NOT NULL, byte_count INTEGER NOT NULL, columns_json TEXT NOT NULL, units_json TEXT NOT NULL,
        provenance TEXT NOT NULL, provenance_source TEXT NOT NULL DEFAULT '', license TEXT NOT NULL DEFAULT '',
        privacy TEXT NOT NULL DEFAULT 'private', retention_deadline INTEGER, parent_version_id TEXT REFERENCES dataset_versions(id),
        transformation TEXT NOT NULL DEFAULT 'upload', created_at INTEGER NOT NULL, deleted_at INTEGER,
        UNIQUE (dataset_id, version));
    CREATE TABLE lineage_edges (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, workspace TEXT NOT NULL, from_type TEXT NOT NULL, from_id TEXT NOT NULL,
        to_type TEXT NOT NULL, to_id TEXT NOT NULL, relation TEXT NOT NULL, created_at INTEGER NOT NULL);
    CREATE INDEX lineage_from ON lineage_edges(workspace, from_type, from_id);
    CREATE INDEX lineage_to ON lineage_edges(workspace, to_type, to_id);
    ALTER TABLE contracts ADD COLUMN dataset_version_id TEXT REFERENCES dataset_versions(id);
    """),
    ('004_workflows', """
    CREATE TABLE workflow_definitions (
        id TEXT PRIMARY KEY, workspace TEXT NOT NULL, owner_id TEXT NOT NULL REFERENCES principals(id), name TEXT NOT NULL,
        version INTEGER NOT NULL, digest TEXT NOT NULL, definition_json TEXT NOT NULL, limits_json TEXT NOT NULL, created_at INTEGER NOT NULL,
        UNIQUE (workspace, digest));
    CREATE TABLE workflow_runs (
        id TEXT PRIMARY KEY, workspace TEXT NOT NULL, definition_id TEXT NOT NULL REFERENCES workflow_definitions(id),
        started_by TEXT NOT NULL REFERENCES principals(id), state TEXT NOT NULL, budget_ceiling INTEGER, bindings_json TEXT NOT NULL,
        estimate_json TEXT NOT NULL, summary_json TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL, finished_at INTEGER);
    CREATE INDEX workflow_runs_ws ON workflow_runs(workspace, state, updated_at);
    CREATE TABLE workflow_nodes (
        run_id TEXT NOT NULL REFERENCES workflow_runs(id), node_id TEXT NOT NULL, type TEXT NOT NULL, state TEXT NOT NULL,
        job_id TEXT REFERENCES jobs(id), contract_id TEXT REFERENCES contracts(id), attempts INTEGER NOT NULL DEFAULT 0,
        blocked_reason TEXT, output_artifact_id TEXT REFERENCES artifacts(id), output_root TEXT, binding_json TEXT, updated_at INTEGER NOT NULL,
        PRIMARY KEY (run_id, node_id));
    ALTER TABLE jobs ADD COLUMN run_id TEXT REFERENCES workflow_runs(id);
    """),
    ('005_campaigns', """
    CREATE TABLE sci_campaigns (
        id TEXT PRIMARY KEY, workspace TEXT NOT NULL, owner_id TEXT NOT NULL REFERENCES principals(id), name TEXT NOT NULL,
        kind TEXT NOT NULL, dataset_version_id TEXT REFERENCES dataset_versions(id), base_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
        definition_json TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL, total_candidates INTEGER NOT NULL,
        adaptive_json TEXT, estimate_json TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, finished_at INTEGER);
    CREATE INDEX campaigns_ws ON sci_campaigns(workspace, state, updated_at);
    CREATE TABLE sci_campaign_candidates (
        campaign_id TEXT NOT NULL REFERENCES sci_campaigns(id), idx INTEGER NOT NULL, params_json TEXT NOT NULL,
        contract_id TEXT REFERENCES contracts(id), job_id TEXT REFERENCES jobs(id), state TEXT NOT NULL, outcome TEXT,
        summary_json TEXT, updated_at INTEGER NOT NULL, PRIMARY KEY (campaign_id, idx));
    """),
    ('006_catalog_quotes_usage', """
    CREATE TABLE services (
        id TEXT PRIMARY KEY, workspace TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, version INTEGER NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', input_schema_json TEXT NOT NULL,
        output_schema_json TEXT NOT NULL, verifier_id TEXT NOT NULL, verifier_digest TEXT NOT NULL, limits_json TEXT NOT NULL,
        privacy_json TEXT NOT NULL, pricing_json TEXT NOT NULL, capabilities_json TEXT NOT NULL, visibility TEXT NOT NULL,
        created_at INTEGER NOT NULL, retired_at INTEGER, UNIQUE (name, version));
    CREATE TABLE quotes (
        id TEXT PRIMARY KEY, workspace TEXT NOT NULL, service_id TEXT NOT NULL REFERENCES services(id), service_revision INTEGER NOT NULL,
        principal_id TEXT NOT NULL REFERENCES principals(id), request_digest TEXT NOT NULL, quantity_max INTEGER NOT NULL,
        amount_max INTEGER NOT NULL, unit TEXT NOT NULL, asset TEXT NOT NULL, network TEXT NOT NULL, pay_to TEXT NOT NULL,
        pricing_revision TEXT NOT NULL, provider_mode TEXT NOT NULL, expires_at INTEGER NOT NULL, accepted_at INTEGER, consumed_at INTEGER,
        state TEXT NOT NULL, binding_json TEXT NOT NULL, created_at INTEGER NOT NULL);
    CREATE INDEX quotes_ws ON quotes(workspace, created_at);
    CREATE TABLE usage_records (
        id TEXT PRIMARY KEY, workspace TEXT NOT NULL, job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id), quote_id TEXT REFERENCES quotes(id),
        service_id TEXT REFERENCES services(id), pricing_revision TEXT NOT NULL, unit TEXT NOT NULL, quantity INTEGER NOT NULL,
        amount_per_unit INTEGER NOT NULL, assessed_charge INTEGER NOT NULL, asset TEXT NOT NULL, calculation_json TEXT NOT NULL,
        state TEXT NOT NULL, statement_json TEXT NOT NULL, signature_hex TEXT NOT NULL, key_id TEXT NOT NULL, created_at INTEGER NOT NULL);
    ALTER TABLE jobs ADD COLUMN quote_id TEXT REFERENCES quotes(id);
    ALTER TABLE contracts ADD COLUMN quote_id TEXT REFERENCES quotes(id);
    CREATE TABLE invoke_sales (
        payment_id TEXT PRIMARY KEY, workspace TEXT NOT NULL, quote_id TEXT NOT NULL REFERENCES quotes(id), job_id TEXT REFERENCES jobs(id),
        resource TEXT NOT NULL, amount TEXT NOT NULL, asset TEXT NOT NULL, network TEXT NOT NULL, pay_to TEXT NOT NULL, provider_mode TEXT NOT NULL,
        state TEXT NOT NULL, transaction_ref TEXT, payer TEXT, requirements_digest TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
    """),
    ('007_agent_policy_grants', """
    CREATE TABLE policy_grants (
        id TEXT PRIMARY KEY, workspace TEXT NOT NULL, issuer_id TEXT NOT NULL REFERENCES principals(id),
        credential_id TEXT NOT NULL UNIQUE REFERENCES credentials(id), policy_json TEXT NOT NULL, digest TEXT NOT NULL,
        state TEXT NOT NULL, counters_json TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
        stopped_at INTEGER, last_denial_json TEXT);
    CREATE INDEX policy_grants_ws ON policy_grants(workspace, created_at);
    """),
    ('008_hierarchical_budgets', """
    CREATE TABLE budget_nodes (
        id TEXT PRIMARY KEY, workspace TEXT NOT NULL, parent_id TEXT REFERENCES budget_nodes(id), kind TEXT NOT NULL, ref_id TEXT NOT NULL,
        ceiling INTEGER NOT NULL, reserved INTEGER NOT NULL DEFAULT 0, committed INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL,
        UNIQUE (kind, ref_id), CHECK (reserved >= 0 AND committed >= 0 AND ceiling >= 0));
    CREATE TABLE budget_reservations (
        id TEXT PRIMARY KEY, workspace TEXT NOT NULL, node_id TEXT NOT NULL REFERENCES budget_nodes(id), amount INTEGER NOT NULL,
        state TEXT NOT NULL, ref_type TEXT NOT NULL, ref_id TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
        UNIQUE (ref_type, ref_id));
    """),
    ('009_workers_quotas', """
    CREATE TABLE workers (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, capabilities_json TEXT NOT NULL, state TEXT NOT NULL,
        registered_at INTEGER NOT NULL, last_heartbeat INTEGER NOT NULL, drained_at INTEGER, current_job_id TEXT);
    CREATE TABLE quotas (
        workspace TEXT NOT NULL, principal_id TEXT NOT NULL, max_queued INTEGER NOT NULL, max_per_minute INTEGER NOT NULL,
        updated_at INTEGER NOT NULL, PRIMARY KEY (workspace, principal_id));
    """),
    ('010_reuse_and_sharing', """
    ALTER TABLE contracts ADD COLUMN inputs_digest TEXT;
    ALTER TABLE jobs ADD COLUMN reused_from TEXT REFERENCES jobs(id);
    CREATE TABLE result_cache (
        workspace TEXT NOT NULL, kind TEXT NOT NULL, inputs_digest TEXT NOT NULL, verifier_digest TEXT NOT NULL,
        job_id TEXT NOT NULL REFERENCES jobs(id), evidence_root TEXT NOT NULL, outcome TEXT, created_at INTEGER NOT NULL,
        PRIMARY KEY (workspace, kind, inputs_digest, verifier_digest));
    CREATE TABLE shares (
        id TEXT PRIMARY KEY, workspace TEXT NOT NULL, job_id TEXT NOT NULL REFERENCES jobs(id), granted_by TEXT NOT NULL REFERENCES principals(id),
        grantee_id TEXT NOT NULL REFERENCES principals(id), fields_json TEXT NOT NULL, created_at INTEGER NOT NULL, revoked_at INTEGER);
    CREATE INDEX shares_job ON shares(job_id, grantee_id);
    """),
    ('011_schedules', """
    CREATE TABLE schedules (
        id TEXT PRIMARY KEY, workspace TEXT NOT NULL, definition_id TEXT NOT NULL REFERENCES workflow_definitions(id), name TEXT NOT NULL,
        bindings_json TEXT NOT NULL, budget_ceiling INTEGER, timezone TEXT NOT NULL, times_json TEXT NOT NULL, overlap TEXT NOT NULL,
        max_runs INTEGER NOT NULL, runs_started INTEGER NOT NULL DEFAULT 0, runs_skipped INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1,
        disabled_reason TEXT, last_run_at INTEGER, next_run_at INTEGER, created_by TEXT NOT NULL REFERENCES principals(id), created_at INTEGER NOT NULL);
    CREATE INDEX schedules_due ON schedules(enabled, next_run_at);
    """),
    ('012_compute_engine', """
    CREATE TABLE compute_runs (
        job_id TEXT PRIMARY KEY REFERENCES jobs(id), workspace TEXT NOT NULL, kind TEXT NOT NULL, manifest_id TEXT NOT NULL, manifest_version INTEGER NOT NULL,
        implementation_digest TEXT NOT NULL, input_digest TEXT NOT NULL, device_policy TEXT NOT NULL, selected_backend TEXT, backend_reason TEXT,
        precision TEXT NOT NULL, phase TEXT NOT NULL, work_total INTEGER NOT NULL, work_committed INTEGER NOT NULL DEFAULT 0, work_computed INTEGER NOT NULL DEFAULT 0,
        chunk_id INTEGER NOT NULL DEFAULT 0, checkpoint_generation INTEGER NOT NULL DEFAULT 0, control TEXT, controlled_at INTEGER, versions_json TEXT,
        telemetry_json TEXT, verification_json TEXT, progress_json TEXT, output_artifact_id TEXT REFERENCES artifacts(id), log_tail TEXT, child_pid INTEGER,
        started_at INTEGER, updated_at INTEGER NOT NULL);
    CREATE TABLE compute_checkpoints (
        id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), generation INTEGER NOT NULL, attempt_generation INTEGER NOT NULL,
        artifact_id TEXT NOT NULL REFERENCES artifacts(id), committed_units INTEGER NOT NULL, boundary_json TEXT NOT NULL, backend TEXT NOT NULL,
        digest TEXT NOT NULL, state TEXT NOT NULL, published_at INTEGER NOT NULL, UNIQUE (job_id, generation));
    CREATE TABLE compute_reservations (
        job_id TEXT PRIMARY KEY REFERENCES jobs(id), worker_id TEXT NOT NULL, device TEXT NOT NULL, slots INTEGER NOT NULL,
        expires_at INTEGER NOT NULL, created_at INTEGER NOT NULL);
    CREATE TABLE compute_work_units (
        job_id TEXT NOT NULL REFERENCES jobs(id), unit_from INTEGER NOT NULL, unit_to INTEGER NOT NULL, generation INTEGER NOT NULL,
        attempt_generation INTEGER NOT NULL, committed_at INTEGER NOT NULL, PRIMARY KEY (job_id, unit_from));
    ALTER TABLE jobs ADD COLUMN hold INTEGER NOT NULL DEFAULT 0;
    """),
    ('013_compute_preemption', """
    ALTER TABLE compute_runs ADD COLUMN preempted_for TEXT;
    ALTER TABLE compute_runs ADD COLUMN preempted_at INTEGER;
    """),
    ('014_local_models', """
    CREATE TABLE model_revisions (
        id TEXT PRIMARY KEY, model_id TEXT NOT NULL, revision TEXT NOT NULL, hub_repo TEXT NOT NULL, local_dir TEXT NOT NULL, architecture TEXT NOT NULL, loader TEXT NOT NULL,
        weight_format TEXT NOT NULL, weight_digest TEXT, tokenizer_digest TEXT, config_json TEXT NOT NULL, license TEXT NOT NULL, operations_json TEXT NOT NULL, context_limit INTEGER,
        embedding_dim INTEGER, pooling TEXT, precision TEXT NOT NULL, resource_estimate_bytes INTEGER, status TEXT NOT NULL, installed INTEGER NOT NULL DEFAULT 0, install_json TEXT,
        description TEXT, registered_by TEXT NOT NULL, created_at INTEGER NOT NULL, retired_at INTEGER, revoked_at INTEGER, revocation_reason TEXT, UNIQUE (model_id, revision));
    CREATE TABLE model_defaults (operation TEXT PRIMARY KEY, revision_id TEXT NOT NULL REFERENCES model_revisions(id), set_by TEXT NOT NULL, evidence_json TEXT, previous_revision_id TEXT, updated_at INTEGER NOT NULL);
    CREATE TABLE model_promotions (id TEXT PRIMARY KEY, operation TEXT NOT NULL, from_revision_id TEXT, to_revision_id TEXT NOT NULL, principal_id TEXT NOT NULL, evidence_json TEXT, action TEXT NOT NULL, created_at INTEGER NOT NULL);
    CREATE TABLE model_runtimes (
        host TEXT NOT NULL, revision_id TEXT NOT NULL REFERENCES model_revisions(id), state TEXT NOT NULL, pid INTEGER, device TEXT, dtype TEXT, estimated_bytes INTEGER, versions_json TEXT,
        loaded_at INTEGER, load_ms INTEGER, last_used_at INTEGER, requests INTEGER NOT NULL DEFAULT 0, desired TEXT, error TEXT, updated_at INTEGER NOT NULL, PRIMARY KEY (host, revision_id));
    CREATE TABLE model_requests (
        job_id TEXT PRIMARY KEY REFERENCES jobs(id), workspace TEXT NOT NULL, kind TEXT NOT NULL, revision_id TEXT REFERENCES model_revisions(id), operation TEXT NOT NULL,
        phase TEXT NOT NULL, host TEXT, request_digest TEXT NOT NULL, max_output_tokens INTEGER, max_items INTEGER, input_tokens INTEGER, output_tokens INTEGER, items INTEGER,
        finish_reason TEXT, queue_seconds INTEGER, load_ms INTEGER, inference_ms INTEGER, versions_json TEXT, segments INTEGER NOT NULL DEFAULT 0, output_chars INTEGER NOT NULL DEFAULT 0,
        output_artifact_id TEXT REFERENCES artifacts(id), attempt_generation INTEGER, usage_json TEXT, error TEXT, started_at INTEGER, updated_at INTEGER NOT NULL);
    CREATE TABLE model_segments (job_id TEXT NOT NULL REFERENCES jobs(id), attempt_generation INTEGER NOT NULL, seq INTEGER NOT NULL, text TEXT NOT NULL, chars INTEGER NOT NULL,
        created_at INTEGER NOT NULL, PRIMARY KEY (job_id, attempt_generation, seq));
    """),
    ('015_private_knowledge', """
    CREATE TABLE knowledge_collections (id TEXT PRIMARY KEY, workspace TEXT NOT NULL, owner_id TEXT NOT NULL, name TEXT NOT NULL, description TEXT, created_at INTEGER NOT NULL, retired_at INTEGER);
    CREATE TABLE knowledge_documents (id TEXT PRIMARY KEY, collection_id TEXT NOT NULL REFERENCES knowledge_collections(id), workspace TEXT NOT NULL, name TEXT NOT NULL, current_version_id TEXT,
        created_at INTEGER NOT NULL, revoked_at INTEGER, revocation_reason TEXT);
    CREATE TABLE knowledge_versions (id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES knowledge_documents(id), collection_id TEXT NOT NULL, workspace TEXT NOT NULL, version INTEGER NOT NULL,
        format TEXT NOT NULL, raw_artifact_id TEXT NOT NULL REFERENCES artifacts(id), text_artifact_id TEXT NOT NULL REFERENCES artifacts(id), text_sha256 TEXT NOT NULL, chars INTEGER NOT NULL,
        parser_id TEXT NOT NULL, warnings_json TEXT, provenance TEXT NOT NULL, source TEXT, license TEXT, chunker_id TEXT NOT NULL, chunk_count INTEGER NOT NULL, created_at INTEGER NOT NULL, deleted_at INTEGER,
        UNIQUE (document_id, version));
    CREATE TABLE knowledge_chunks (version_id TEXT NOT NULL REFERENCES knowledge_versions(id), ordinal INTEGER NOT NULL, start_byte INTEGER NOT NULL, end_byte INTEGER NOT NULL, heading TEXT, sha256 TEXT NOT NULL,
        chars INTEGER NOT NULL, PRIMARY KEY (version_id, ordinal));
    CREATE TABLE knowledge_indexes (id TEXT PRIMARY KEY, collection_id TEXT NOT NULL REFERENCES knowledge_collections(id), workspace TEXT NOT NULL, version INTEGER NOT NULL, state TEXT NOT NULL,
        stale_reason TEXT, model_revision_id TEXT NOT NULL REFERENCES model_revisions(id), chunker_id TEXT NOT NULL, embedding_dim INTEGER, normalization TEXT, similarity TEXT,
        document_versions_json TEXT NOT NULL, chunk_count INTEGER NOT NULL, vectors_artifact_id TEXT REFERENCES artifacts(id), index_job_id TEXT, built_at INTEGER, error TEXT, created_at INTEGER NOT NULL,
        updated_at INTEGER, UNIQUE (collection_id, version));
    CREATE TABLE knowledge_answers (id TEXT PRIMARY KEY, workspace TEXT NOT NULL, collection_id TEXT NOT NULL, index_id TEXT, job_id TEXT NOT NULL, principal_id TEXT NOT NULL, mode TEXT NOT NULL,
        question_sha256 TEXT NOT NULL, status TEXT NOT NULL, sources_json TEXT NOT NULL, citations_json TEXT NOT NULL, invalidated_at INTEGER, invalidation_reason TEXT, created_at INTEGER NOT NULL);
    """),
    ('016_calibration', """
    ALTER TABLE compute_runs ADD COLUMN duration_ms INTEGER;
    ALTER TABLE compute_runs ADD COLUMN compute_ms INTEGER;
    CREATE TABLE calibration_datasets (id TEXT PRIMARY KEY, workspace TEXT NOT NULL, owner_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL, columns_json TEXT NOT NULL,
        units_json TEXT NOT NULL, rows_artifact_id TEXT NOT NULL REFERENCES artifacts(id), row_count INTEGER NOT NULL, digest TEXT NOT NULL, policy_json TEXT NOT NULL, scope_json TEXT, created_at INTEGER NOT NULL);
    CREATE TABLE calibration_models (id TEXT PRIMARY KEY, workspace TEXT NOT NULL, dataset_id TEXT NOT NULL, job_id TEXT NOT NULL REFERENCES jobs(id), kind TEXT NOT NULL, state TEXT NOT NULL, target TEXT NOT NULL,
        features_json TEXT NOT NULL, scope_json TEXT, manifest_artifact_id TEXT NOT NULL REFERENCES artifacts(id), metrics_json TEXT NOT NULL, domain_json TEXT NOT NULL, warnings_json TEXT NOT NULL,
        implementation_digest TEXT NOT NULL, verification_passed INTEGER NOT NULL, approved_by TEXT, approved_at INTEGER, retired_at INTEGER, created_at INTEGER NOT NULL);
    CREATE TABLE calibration_defaults (scope TEXT PRIMARY KEY, workspace TEXT NOT NULL, model_id TEXT NOT NULL REFERENCES calibration_models(id), set_by TEXT NOT NULL, previous_model_id TEXT, evidence_json TEXT, updated_at INTEGER NOT NULL);
    """),
    ('017_verification', """
    CREATE TABLE verification_jobs (id TEXT PRIMARY KEY, workspace TEXT NOT NULL, target_job_id TEXT NOT NULL REFERENCES jobs(id), requested_by TEXT NOT NULL, class TEXT NOT NULL, params_json TEXT NOT NULL,
        state TEXT NOT NULL, preview_json TEXT NOT NULL, audit_job_id TEXT, replica_job_id TEXT, challenge_json TEXT, result_commitment TEXT NOT NULL, result_json TEXT, statement_json TEXT, signature_hex TEXT,
        key_id TEXT, dispute_json TEXT, resolution_json TEXT, resolved_by TEXT, resolved_at INTEGER, created_at INTEGER NOT NULL, finished_at INTEGER);
    """),
    ('018_federation', """
    ALTER TABLE jobs ADD COLUMN location_policy TEXT;
    CREATE TABLE nodes (id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT, public_key_hex TEXT NOT NULL, secret_hash TEXT NOT NULL, capabilities_json TEXT NOT NULL, devices_json TEXT NOT NULL,
        devices_reported_json TEXT, workspaces_json TEXT NOT NULL, artifact_scope TEXT NOT NULL, state TEXT NOT NULL, enrolled_by TEXT NOT NULL, enrolled_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
        revoked_at INTEGER, revocation_reason TEXT, credential_rotated_at INTEGER, last_seen INTEGER, last_request_ts INTEGER, last_request_nonce TEXT, current_job_id TEXT, observed_json TEXT, versions_json TEXT, trust_json TEXT);
    CREATE TABLE node_uploads (id TEXT PRIMARY KEY, node_id TEXT NOT NULL REFERENCES nodes(id), job_id TEXT NOT NULL, attempt_generation INTEGER NOT NULL, role TEXT NOT NULL, expected_sha256 TEXT NOT NULL,
        total_bytes INTEGER NOT NULL, received_bytes INTEGER NOT NULL, path TEXT NOT NULL, state TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, expires_at INTEGER NOT NULL);
    CREATE TABLE node_transfers (id TEXT PRIMARY KEY, node_id TEXT NOT NULL REFERENCES nodes(id), job_id TEXT NOT NULL, attempt_generation INTEGER NOT NULL, role TEXT NOT NULL, direction TEXT NOT NULL,
        bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, state TEXT NOT NULL, created_at INTEGER NOT NULL, completed_at INTEGER);
    """),
    ('019_approvals', """
    CREATE TABLE approvals (id TEXT PRIMARY KEY, workspace TEXT NOT NULL, action TEXT NOT NULL, content_json TEXT NOT NULL, content_digest TEXT NOT NULL, revision_digest TEXT NOT NULL, state TEXT NOT NULL,
        proposed_by TEXT NOT NULL, approved_by TEXT, note TEXT, decision_note TEXT, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, decided_at INTEGER, applied_at INTEGER, apply_result_json TEXT);
    """),
    ('020_verification_policies', """
    CREATE TABLE verification_policies (id TEXT PRIMARY KEY, workspace TEXT NOT NULL, name TEXT NOT NULL, version INTEGER NOT NULL, class TEXT NOT NULL, params_json TEXT NOT NULL, max_work INTEGER NOT NULL,
        verifier_digest TEXT NOT NULL, scope TEXT NOT NULL, created_by TEXT NOT NULL, created_at INTEGER NOT NULL, retired_at INTEGER, UNIQUE (workspace, name, version));
    """),
    ('021_evaluation_registry', """
    CREATE TABLE evaluation_suites (id TEXT PRIMARY KEY, workspace TEXT NOT NULL, name TEXT NOT NULL, version INTEGER NOT NULL, items_json TEXT NOT NULL, digest TEXT NOT NULL, threshold_percent INTEGER NOT NULL,
        created_by TEXT NOT NULL, created_at INTEGER NOT NULL, UNIQUE (workspace, name, version));
    CREATE TABLE evaluation_runs (id TEXT PRIMARY KEY, workspace TEXT NOT NULL, suite_id TEXT NOT NULL REFERENCES evaluation_suites(id), model_revision_id TEXT NOT NULL, jobs_json TEXT NOT NULL, state TEXT NOT NULL,
        results_json TEXT, passed INTEGER, total INTEGER, percent INTEGER, started_by TEXT NOT NULL, created_at INTEGER NOT NULL, scored_at INTEGER);
    """),
    ('022_notebooks', """
    CREATE TABLE notebooks (id TEXT PRIMARY KEY, workspace TEXT NOT NULL, name TEXT NOT NULL, owner_id TEXT NOT NULL, created_at INTEGER NOT NULL);
    CREATE TABLE notebook_versions (id TEXT PRIMARY KEY, notebook_id TEXT NOT NULL REFERENCES notebooks(id), version INTEGER NOT NULL, blocks_json TEXT NOT NULL, links_json TEXT NOT NULL, digest TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL, created_at INTEGER NOT NULL, UNIQUE (notebook_id, version));
    """),
    ('023_warmup_batching', """
    ALTER TABLE model_runtimes ADD COLUMN warm INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE model_runtimes ADD COLUMN drain_reason TEXT;
    ALTER TABLE model_runtimes ADD COLUMN drained_at INTEGER;
    """),
    ('024_agent_plans', """
    CREATE TABLE agent_plans (id TEXT PRIMARY KEY, workspace TEXT NOT NULL, principal_id TEXT NOT NULL, grant_id TEXT, goal_sha256 TEXT, draft_json TEXT NOT NULL, digest TEXT, validation_json TEXT NOT NULL,
        assist_json TEXT NOT NULL, state TEXT NOT NULL, execution_json TEXT, created_at INTEGER NOT NULL, accepted_at INTEGER, accepted_by TEXT);
    """),
    ('025_metered_upto', """
    ALTER TABLE quotes ADD COLUMN scheme TEXT NOT NULL DEFAULT 'exact';
    CREATE TABLE metered_settlements (payment_id TEXT PRIMARY KEY, workspace TEXT NOT NULL, quote_id TEXT NOT NULL REFERENCES quotes(id), job_id TEXT REFERENCES jobs(id), resource TEXT NOT NULL,
        max_amount TEXT NOT NULL, final_amount TEXT, asset TEXT NOT NULL, network TEXT NOT NULL, pay_to TEXT NOT NULL, provider_mode TEXT NOT NULL, scheme TEXT NOT NULL, state TEXT NOT NULL,
        payload_json TEXT NOT NULL, requirements_json TEXT NOT NULL, extensions_json TEXT, requirements_digest TEXT NOT NULL, transaction_ref TEXT, payer TEXT, error TEXT,
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, settled_at INTEGER);
    """),
    ('026_document_intelligence', """
    CREATE TABLE document_imports (id TEXT PRIMARY KEY, workspace TEXT NOT NULL, owner_id TEXT NOT NULL REFERENCES principals(id), collection_id TEXT REFERENCES knowledge_collections(id), name TEXT NOT NULL,
        declared_format TEXT NOT NULL, source_artifact_id TEXT NOT NULL REFERENCES artifacts(id), content_sha256 TEXT NOT NULL, byte_count INTEGER NOT NULL, policy_json TEXT NOT NULL, state TEXT NOT NULL, stage TEXT,
        progress_json TEXT, attempt INTEGER NOT NULL DEFAULT 0, job_id TEXT REFERENCES jobs(id), extraction_id TEXT, document_id TEXT, version_id TEXT, page_count INTEGER, error_json TEXT,
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, removed_at INTEGER);
    CREATE INDEX document_imports_ws ON document_imports(workspace, created_at);
    CREATE TABLE document_extractions (id TEXT PRIMARY KEY, import_id TEXT NOT NULL REFERENCES document_imports(id), workspace TEXT NOT NULL, attempt INTEGER NOT NULL, job_id TEXT REFERENCES jobs(id), parser_json TEXT NOT NULL,
        policy_json TEXT NOT NULL, source_sha256 TEXT NOT NULL, pages_artifact_id TEXT REFERENCES artifacts(id), text_artifact_id TEXT REFERENCES artifacts(id), tables_artifact_id TEXT REFERENCES artifacts(id),
        page_map_json TEXT NOT NULL, previews_json TEXT NOT NULL, page_count INTEGER NOT NULL, excluded_pages INTEGER NOT NULL, ocr_pages INTEGER NOT NULL, table_count INTEGER NOT NULL, warnings_json TEXT NOT NULL,
        active_content_json TEXT NOT NULL, created_at INTEGER NOT NULL);
    CREATE TABLE document_tables (id TEXT PRIMARY KEY, extraction_id TEXT NOT NULL REFERENCES document_extractions(id), import_id TEXT NOT NULL, workspace TEXT NOT NULL, ordinal INTEGER NOT NULL, page_index INTEGER NOT NULL,
        method TEXT NOT NULL, n_rows INTEGER NOT NULL, n_cols INTEGER NOT NULL, header_row INTEGER, region_json TEXT, table_json TEXT NOT NULL, table_sha256 TEXT NOT NULL, ambiguity_json TEXT NOT NULL, supported_form TEXT NOT NULL,
        continuation_json TEXT, created_at INTEGER NOT NULL);
    CREATE TABLE table_annotations (id TEXT PRIMARY KEY, table_id TEXT NOT NULL REFERENCES document_tables(id), workspace TEXT NOT NULL, author_id TEXT NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL, created_at INTEGER NOT NULL);
    CREATE TABLE dataset_mappings (id TEXT PRIMARY KEY, table_id TEXT NOT NULL REFERENCES document_tables(id), workspace TEXT NOT NULL, author_id TEXT NOT NULL, mapping_json TEXT NOT NULL, annotations_digest TEXT NOT NULL,
        state TEXT NOT NULL, preview_json TEXT, digest TEXT NOT NULL, dataset_version_id TEXT, target TEXT NOT NULL, confirmed_by TEXT, confirmed_at INTEGER, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
    ALTER TABLE knowledge_chunks ADD COLUMN page_index INTEGER;
    ALTER TABLE knowledge_chunks ADD COLUMN region_json TEXT;
    """),
    ('027_generation_batching', """
    ALTER TABLE model_requests ADD COLUMN batch_id TEXT;
    ALTER TABLE model_requests ADD COLUMN batch_position INTEGER;
    CREATE TABLE model_batches (id TEXT PRIMARY KEY, workspace TEXT NOT NULL, host TEXT NOT NULL, revision_id TEXT NOT NULL, mode TEXT NOT NULL, members INTEGER NOT NULL, member_jobs_json TEXT NOT NULL,
        prompt_tokens INTEGER, max_new_tokens INTEGER, decode_steps INTEGER, padded_prompt_length INTEGER, ms INTEGER, kv_estimate_bytes INTEGER, cuda_peak_delta_bytes INTEGER, cancelled_members INTEGER NOT NULL DEFAULT 0,
        outcome_json TEXT, started_at INTEGER NOT NULL, finished_at INTEGER);
    """),
    ('028_agent_intents', """
    CREATE TABLE agent_intents (id TEXT PRIMARY KEY, workspace TEXT NOT NULL, principal_id TEXT NOT NULL, request_sha256 TEXT NOT NULL, request_json TEXT NOT NULL, intent_json TEXT NOT NULL, state TEXT NOT NULL,
        continuation_token TEXT, plan_id TEXT, attempts INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
    """),
    ('029_analyses', """
    CREATE TABLE analyses (id TEXT PRIMARY KEY, workspace TEXT NOT NULL, name TEXT NOT NULL, owner_id TEXT NOT NULL, notebook_id TEXT NOT NULL REFERENCES notebooks(id), created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
    CREATE TABLE analysis_versions (analysis_id TEXT NOT NULL REFERENCES analyses(id), version INTEGER NOT NULL, stale_json TEXT NOT NULL, changed_json TEXT NOT NULL, frozen_at INTEGER, frozen_by TEXT, freeze_reason TEXT,
        created_by TEXT NOT NULL, created_at INTEGER NOT NULL, PRIMARY KEY (analysis_id, version));
    CREATE TABLE analysis_reports (id TEXT PRIMARY KEY, analysis_id TEXT NOT NULL REFERENCES analyses(id), workspace TEXT NOT NULL, version INTEGER NOT NULL, mode TEXT NOT NULL, digest TEXT NOT NULL, markdown TEXT NOT NULL,
        html TEXT NOT NULL, manifest_json TEXT NOT NULL, flags_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at INTEGER NOT NULL);
    CREATE TABLE analysis_projections (id TEXT PRIMARY KEY, report_id TEXT NOT NULL REFERENCES analysis_reports(id), workspace TEXT NOT NULL, scope_json TEXT NOT NULL, digest TEXT NOT NULL, bundle_json TEXT NOT NULL,
        created_by TEXT NOT NULL, created_at INTEGER NOT NULL);
    """),
    ('030_packages', """
    CREATE TABLE packages (id TEXT PRIMARY KEY, workspace TEXT NOT NULL, name TEXT NOT NULL, version INTEGER NOT NULL, digest TEXT NOT NULL, manifest_json TEXT NOT NULL, state TEXT NOT NULL, created_by TEXT NOT NULL,
        created_at INTEGER NOT NULL, retired_at INTEGER, UNIQUE (workspace, name, version));
    CREATE TABLE package_quotes (id TEXT PRIMARY KEY, workspace TEXT NOT NULL, package_id TEXT NOT NULL REFERENCES packages(id), workflow_id TEXT NOT NULL, principal_id TEXT NOT NULL, scheme TEXT NOT NULL, digest TEXT NOT NULL,
        binding_json TEXT NOT NULL, components_json TEXT NOT NULL, amount_max INTEGER NOT NULL, fixed_amount INTEGER NOT NULL, metered_amount INTEGER NOT NULL, expires_at INTEGER NOT NULL, state TEXT NOT NULL, created_at INTEGER NOT NULL, consumed_at INTEGER);
    CREATE TABLE package_runs (id TEXT PRIMARY KEY, workspace TEXT NOT NULL, package_id TEXT NOT NULL REFERENCES packages(id), kind TEXT NOT NULL, run_id TEXT, job_id TEXT, quote_id TEXT, state TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 1,
        delivery_json TEXT NOT NULL, verification_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
    CREATE INDEX package_runs_job ON package_runs(job_id);
    """),
    ('031_reconciliation', """
    CREATE TABLE reconciliations (id TEXT PRIMARY KEY, workspace TEXT NOT NULL, quantity TEXT NOT NULL, unit TEXT NOT NULL, record_json TEXT NOT NULL, digest TEXT NOT NULL, created_by TEXT NOT NULL, created_at INTEGER NOT NULL);
    CREATE TABLE measurement_requests (id TEXT PRIMARY KEY, workspace TEXT NOT NULL, quantity TEXT NOT NULL, unit TEXT NOT NULL, record_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at INTEGER NOT NULL);
    """),
    ('032_work_terms', """
    CREATE TABLE work_terms (id TEXT PRIMARY KEY, workspace TEXT NOT NULL, requester_id TEXT NOT NULL REFERENCES principals(id), state TEXT NOT NULL CHECK (state IN ('draft','frozen','superseded','withdrawn')), version INTEGER NOT NULL DEFAULT 1, lineage_id TEXT NOT NULL, previous_id TEXT REFERENCES work_terms(id), terms_json TEXT NOT NULL, digest TEXT UNIQUE, draft_digest TEXT NOT NULL, contract_id TEXT REFERENCES contracts(id), proposed_by TEXT, expires_at INTEGER, frozen_at INTEGER, superseded_by TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
    CREATE INDEX work_terms_lineage ON work_terms (lineage_id, version);
    CREATE TABLE work_evaluations (id TEXT PRIMARY KEY, workspace TEXT NOT NULL, terms_id TEXT NOT NULL REFERENCES work_terms(id), milestone_key TEXT NOT NULL, job_id TEXT, evaluation_json TEXT NOT NULL, decision_candidate TEXT NOT NULL, science TEXT NOT NULL, execution TEXT NOT NULL, payment_class TEXT NOT NULL, evaluated_by TEXT NOT NULL, created_at INTEGER NOT NULL);
    """),
]


def open_db(path, create=False):
    path = os.fspath(path)
    if not create and not os.path.exists(path):
        raise FileNotFoundError('database missing')
    if create:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.close(fd)
    info = os.stat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise PermissionError('database must be a private regular file')
    db = sqlite3.connect(path, timeout=15, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    db.execute('PRAGMA busy_timeout=15000')
    return db


def migrate(path):
    """Apply pending migrations in order; returns the applied list."""
    db = open_db(path, create=True)
    applied = []
    try:
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at INTEGER NOT NULL)')
        done = {r[0] for r in db.execute('SELECT name FROM schema_migrations')}
        for name, sql in MIGRATIONS:
            if name in done:
                continue
            db.executescript(sql) if False else [db.execute(stmt) for stmt in _statements(sql)]
            db.execute('INSERT INTO schema_migrations VALUES (?, ?)', (name, int(time.time())))
            applied.append(name)
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise
    finally:
        db.close()
    return applied


def _statements(sql):
    return [s.strip() for s in sql.split(';') if s.strip()]


def schema_version(path):
    db = open_db(path)
    try:
        rows = [r[0] for r in db.execute('SELECT name FROM schema_migrations ORDER BY name')]
    finally:
        db.close()
    return rows


def check_schema(path, allow_older=False):
    """Structural check before opening restored or foreign state for writes. With allow_older, a database at an OLDER
    schema that is an exact prefix of this revision's migration list is accepted (it can be migrated forward, e.g. a backup
    taken before an upgrade restored by the upgraded code); a newer, unknown or out-of-order schema is always refused."""
    expected = [name for name, _ in MIGRATIONS]
    actual = schema_version(path)
    if actual != expected and not (allow_older and actual and actual == expected[:len(actual)]):
        raise RuntimeError('schema version mismatch: ' + ','.join(actual) + ' vs ' + ','.join(expected))
    db = open_db(path)
    try:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('integrity check failed')
        if db.execute('PRAGMA foreign_key_check').fetchall():
            raise RuntimeError('foreign key check failed')
    finally:
        db.close()
    return actual


class Database:
    def __init__(self, path):
        self.path = os.fspath(path)

    @contextmanager
    def tx(self):
        db = open_db(self.path)
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise
        finally:
            db.close()

    @contextmanager
    def read(self):
        db = open_db(self.path)
        try:
            yield db
        finally:
            db.close()


def now():
    return int(time.time())
