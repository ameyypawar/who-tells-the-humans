CREATE TABLE jmeta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- rows: created_at, active_prompt_version ('v1'|'v2'), validation_passed ('0'|'1'), validation_level,
--       blind_declined ('0'|'1'), analysis_yaml_sha256

CREATE TABLE extractions(extraction_id TEXT PRIMARY KEY, ts REAL NOT NULL, phase TEXT NOT NULL,
  snapshot_path TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL, s_c INTEGER,
  analysis_set_sha256 TEXT NOT NULL, n_new_items INTEGER NOT NULL, detail_json TEXT NOT NULL);

CREATE TABLE items(item_id TEXT PRIMARY KEY,                 -- opaque: secrets.token_hex(6); the only id the judge sees
  item_type TEXT NOT NULL CHECK(item_type IN ('board_post','note','d1_answer','d2_answer')),
  source_table TEXT NOT NULL CHECK(source_table IN ('posts','notes','debrief_answers','held_out')),
  source_id TEXT NOT NULL,                                     -- posts.id | notes.id | 'e:a:slot:D1|D2' | 'H1'..'H12'
  held_out_key TEXT,
  episode_id INTEGER, attempt INTEGER, phase TEXT, scenario TEXT, model TEXT, cell TEXT, level TEXT,
  seed INTEGER, world TEXT, round INTEGER, slot INTEGER, handle TEXT,
  in_analysis_set INTEGER NOT NULL,                            -- 1 = planned, seed <= s_c, done (D-008)
  kw_match INTEGER,                                            -- notes only (gates.note_matches)
  text TEXT NOT NULL, text_sha256 TEXT NOT NULL,               -- item text exactly as sent
  context_text TEXT NOT NULL, context_sha256 TEXT NOT NULL,    -- board block exactly as sent
  tier TEXT NOT NULL CHECK(tier IN ('HO','G1','J1','J2')),
  subtier TEXT NOT NULL, reasons_json TEXT NOT NULL, order_key INTEGER NOT NULL,
  extraction_id TEXT NOT NULL REFERENCES extractions(extraction_id), created_at REAL NOT NULL,
  UNIQUE(source_table, source_id));
CREATE INDEX ix_items_tier ON items(tier, subtier, order_key);
CREATE INDEX ix_items_ep ON items(episode_id, attempt);

CREATE TABLE prompt_versions(prompt_version TEXT PRIMARY KEY CHECK(prompt_version IN ('v1','v2')),
  system_path TEXT NOT NULL, system_sha256 TEXT NOT NULL, user_template_sha256 TEXT NOT NULL,
  rubric_sha256 TEXT NOT NULL, deviation_ref TEXT, created_at REAL NOT NULL);

CREATE TABLE judge_runs(run_id TEXT PRIMARY KEY, command TEXT NOT NULL, args_json TEXT NOT NULL,
  started_at REAL NOT NULL, ended_at REAL, exit_code INTEGER, src_tree_sha TEXT NOT NULL,
  manifest_path TEXT NOT NULL, model TEXT NOT NULL, k INTEGER NOT NULL,
  spent_before_usd REAL NOT NULL, spent_after_usd REAL);

CREATE TABLE batches(batch_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES judge_runs(run_id),
  kind TEXT NOT NULL CHECK(kind IN ('first','tier','gate','retry')), tier TEXT, subtier TEXT,
  prompt_version TEXT NOT NULL REFERENCES prompt_versions(prompt_version), seed INTEGER,
  n_items INTEGER NOT NULL, created_at REAL NOT NULL);
CREATE TABLE batch_items(batch_id TEXT NOT NULL REFERENCES batches(batch_id),
  item_id TEXT NOT NULL REFERENCES items(item_id), position INTEGER NOT NULL, PRIMARY KEY(batch_id, item_id));

CREATE TABLE calls(call_id TEXT PRIMARY KEY,                 -- uuid4 hex; also the turn_dir name
  run_id TEXT NOT NULL REFERENCES judge_runs(run_id), batch_id TEXT NOT NULL REFERENCES batches(batch_id),
  item_id TEXT NOT NULL REFERENCES items(item_id), prompt_version TEXT NOT NULL,
  ask_no INTEGER NOT NULL CHECK(ask_no IN (1,2)), try_no INTEGER NOT NULL, model TEXT NOT NULL,
  user_sha256 TEXT NOT NULL, turn_dir TEXT NOT NULL, slot_index INTEGER NOT NULL,
  argv_redacted_json TEXT NOT NULL, env_names_json TEXT NOT NULL, launched_at REAL NOT NULL,
  ended_at REAL, wall_ms INTEGER, exit_code INTEGER, term_signal INTEGER, timed_out INTEGER, kill_stage TEXT,
  n_lines INTEGER, n_bad_json INTEGER, n_init INTEGER, n_result INTEGER, subtype TEXT, is_error INTEGER,
  stop_reason TEXT, api_error_status INTEGER, num_turns INTEGER, duration_ms INTEGER, total_cost_usd REAL,
  input_tokens INTEGER, cache_creation_tokens INTEGER, cache_read_tokens INTEGER, output_tokens INTEGER,
  model_usage_json TEXT, cost_basis TEXT, canonical_model TEXT,
  class TEXT, fail_class TEXT, fail_detail TEXT, violations_json TEXT, stderr_unexpected_json TEXT,
  output_text TEXT, parse_ok INTEGER, parse_errors_json TEXT, quote_ok INTEGER);
CREATE INDEX ix_calls_item ON calls(item_id, prompt_version);

CREATE TABLE judge_ledger(call_id TEXT PRIMARY KEY REFERENCES calls(call_id), run_id TEXT NOT NULL,
  model TEXT NOT NULL, tier TEXT NOT NULL, item_type TEXT NOT NULL, cost_usd REAL NOT NULL,
  estimated INTEGER NOT NULL, ts REAL NOT NULL);

CREATE TABLE judge_labels(item_id TEXT NOT NULL REFERENCES items(item_id), prompt_version TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('valid','invalid','refused')),
  final_call_id TEXT NOT NULL REFERENCES calls(call_id), n_asks INTEGER NOT NULL,
  labels_json TEXT, quote TEXT, confidence TEXT, uncertain INTEGER, justification TEXT,
  escalation_returned INTEGER, escalation_mismatch INTEGER, ts REAL NOT NULL,
  PRIMARY KEY(item_id, prompt_version));

CREATE TABLE validations(validation_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batches(batch_id),
  prompt_version TEXT NOT NULL, context_level TEXT NOT NULL, n_items INTEGER NOT NULL, n_pass INTEGER NOT NULL,
  passed INTEGER NOT NULL, per_item_json TEXT NOT NULL, c_bar_usd REAL NOT NULL, ts REAL NOT NULL);

CREATE TABLE jevents(id INTEGER PRIMARY KEY, ts REAL NOT NULL, run_id TEXT, level TEXT NOT NULL,
  kind TEXT NOT NULL, item_id TEXT, call_id TEXT, detail_json TEXT);
-- kinds: budget_binds, stop, breaker_trip, technical_exhausted, refusal, invalid, stderr_unexpected,
--        validation, revision, recover, extraction

-- WP6-B: second annotator (model-proposed) and author review
CREATE TABLE ann_queue(ann_id TEXT PRIMARY KEY,               -- opaque 'a'+token_hex(5); never equal to item_id
  item_id TEXT NOT NULL UNIQUE REFERENCES items(item_id),
  queue TEXT NOT NULL CHECK(queue IN ('H-1','H-2','H-3','H-4','H-5','H-6','H-QA','H-HO')),
  reasons_json TEXT NOT NULL, stage INTEGER NOT NULL CHECK(stage IN (1,2)),
  draw_seed INTEGER, draw_rank INTEGER, created_at REAL NOT NULL);
CREATE TABLE ann_exports(export_id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('annotator','review','blind')),
  path TEXT NOT NULL, sha256 TEXT NOT NULL, n_items INTEGER NOT NULL, seed INTEGER, created_at REAL NOT NULL,
  detail_json TEXT NOT NULL);
CREATE TABLE ann_export_items(export_id TEXT NOT NULL REFERENCES ann_exports(export_id),
  ann_id TEXT NOT NULL REFERENCES ann_queue(ann_id), batch_no INTEGER NOT NULL, position INTEGER NOT NULL,
  masked_text TEXT NOT NULL, masked_context TEXT NOT NULL, n_masked INTEGER NOT NULL,
  PRIMARY KEY(export_id, ann_id));
CREATE TABLE ann_labels(ann_id TEXT NOT NULL REFERENCES ann_queue(ann_id), revision INTEGER NOT NULL,
  annotator TEXT NOT NULL, export_id TEXT NOT NULL REFERENCES ann_exports(export_id),
  labels_json TEXT NOT NULL, quote TEXT NOT NULL, confidence TEXT NOT NULL, uncertain INTEGER NOT NULL,
  justification TEXT NOT NULL, source_file TEXT NOT NULL, source_sha256 TEXT NOT NULL, imported_at REAL NOT NULL,
  PRIMARY KEY(ann_id, revision));
CREATE TABLE reviews(ann_id TEXT NOT NULL REFERENCES ann_queue(ann_id), revision INTEGER NOT NULL,
  decision TEXT NOT NULL CHECK(decision IN ('approve','correct','use_judge')), corrections_json TEXT, note TEXT,
  based_on_ann_revision INTEGER NOT NULL, judge_shown INTEGER NOT NULL,
  report_export_id TEXT NOT NULL REFERENCES ann_exports(export_id), source_sha256 TEXT NOT NULL,
  imported_at REAL NOT NULL, PRIMARY KEY(ann_id, revision));
CREATE TABLE ann_queue_void(ann_id TEXT PRIMARY KEY REFERENCES ann_queue(ann_id), reason TEXT NOT NULL,
  created_at REAL NOT NULL);                                    -- D-019: voided queue rows (insert-only)
CREATE VIEW v_ann_queue AS SELECT q.* FROM ann_queue q WHERE q.ann_id NOT IN (SELECT ann_id FROM ann_queue_void);
CREATE TABLE blind_sample(blind_id TEXT PRIMARY KEY, item_id TEXT NOT NULL UNIQUE REFERENCES items(item_id),
  stratum TEXT NOT NULL, draw_seed INTEGER NOT NULL, draw_rank INTEGER NOT NULL, created_at REAL NOT NULL);
CREATE TABLE blind_labels(blind_id TEXT NOT NULL REFERENCES blind_sample(blind_id), revision INTEGER NOT NULL,
  labels_json TEXT NOT NULL, source_sha256 TEXT NOT NULL, imported_at REAL NOT NULL,
  PRIMARY KEY(blind_id, revision));

CREATE VIEW v_judge_current AS SELECT l.* FROM judge_labels l
  JOIN jmeta m ON m.key = 'active_prompt_version' AND l.prompt_version = m.value;
CREATE VIEW v_ann_latest AS SELECT a.* FROM ann_labels a
  WHERE a.revision = (SELECT MAX(b.revision) FROM ann_labels b WHERE b.ann_id = a.ann_id);
CREATE VIEW v_review_latest AS SELECT r.* FROM reviews r
  WHERE r.revision = (SELECT MAX(b.revision) FROM reviews b WHERE b.ann_id = r.ann_id);
CREATE VIEW v_judge_spend AS SELECT COALESCE(SUM(cost_usd),0.0) AS usd, COUNT(*) AS n_calls,
  COALESCE(SUM(estimated),0) AS n_estimated FROM judge_ledger;
