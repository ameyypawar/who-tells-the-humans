-- wtth schema, version 1 (research/41 section 2.4 + research/43 B1/B8 + research/42 section 4 additions).
-- DDL is run only by db.create_db(), which then adds the generated triggers (never DELETE, selective
-- UPDATE guards), sets journal_mode=WAL and user_version=1. Times are unix seconds (REAL).
-- JSON columns hold textnorm.canon_json output.
--
-- ADDITIONS over research/41 (all nullable or defaulted, so 41's column lists still insert cleanly):
--   turns.try_no (B1), turns.k_at_launch, turns.max_turns_cap, turns.max_budget_usd, turns.api_key_source,
--   turns.canonical_model                                                         (42 section 4.3)
--   notes.kw_hits_json (B8: {"subject":[..],"message":[..]} over the stored, cut fields)
--   episodes.runbook, c2_line_ref, c_real_note, forged_line_ref                    (42 section 4.2)
--   stream_tool_uses.error_kind, reconciled                                         (42 section 4.4)
--   exposures.checks_json                                                           (42 section 4.9)
--   table gate_log                                                                  (42 section 4.13)

CREATE TABLE runs(run_id TEXT PRIMARY KEY, phase TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN('live','fake')),
  started_at REAL NOT NULL, ended_at REAL, exit_code INTEGER, manifest_sha TEXT NOT NULL, manifest_json TEXT NOT NULL);

CREATE TABLE episodes(episode_id INTEGER PRIMARY KEY, episode_key TEXT NOT NULL UNIQUE,  -- phase/scenario/model/cell/level/s<seed>
  phase TEXT NOT NULL, scenario TEXT NOT NULL, model TEXT NOT NULL, cell TEXT NOT NULL, level TEXT NOT NULL,
  seed INTEGER NOT NULL, world TEXT NOT NULL CHECK(world IN('R0','R1')),
  handle_idx INTEGER NOT NULL, ticket_idx INTEGER NOT NULL, paraphrase_idx INTEGER NOT NULL,
  handles_json TEXT NOT NULL, ticket_sets_json TEXT NOT NULL, queue_pos INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN('queued','running','done','failed')),
  final_attempt INTEGER, n_attempts INTEGER NOT NULL DEFAULT 0, not_before REAL, fail_reason TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL,
  runbook TEXT, c2_line_ref TEXT, c_real_note INTEGER, forged_line_ref TEXT);

CREATE TABLE attempts(episode_id INTEGER NOT NULL REFERENCES episodes(episode_id), attempt INTEGER NOT NULL,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  status TEXT NOT NULL CHECK(status IN('running','done','failed','abandoned')),
  started_at REAL NOT NULL, finished_at REAL, fail_class TEXT, fail_detail TEXT, PRIMARY KEY(episode_id, attempt));
CREATE UNIQUE INDEX ux_attempt_done    ON attempts(episode_id) WHERE status='done';
CREATE UNIQUE INDEX ux_attempt_running ON attempts(episode_id) WHERE status='running';

CREATE TABLE turns(turn_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, episode_id INTEGER NOT NULL, attempt INTEGER NOT NULL,
  turn_type TEXT NOT NULL CHECK(turn_type IN('witness','d1','d2','probe','judge')),
  round INTEGER, slot INTEGER, handle TEXT, model TEXT NOT NULL, slot_index INTEGER NOT NULL, turn_dir TEXT NOT NULL,
  argv_redacted_json TEXT NOT NULL, env_names_json TEXT NOT NULL, audit_bodies INTEGER NOT NULL,
  launched_at REAL NOT NULL, ended_at REAL, wall_ms INTEGER, exit_code INTEGER, term_signal INTEGER,
  timed_out INTEGER, kill_stage TEXT, peak_rss_kb INTEGER,
  n_lines INTEGER, n_nonjson INTEGER, n_bad_json INTEGER, truncated_tail INTEGER, n_init INTEGER, n_result INTEGER,
  subtype TEXT, is_error INTEGER, stop_reason TEXT, terminal_reason TEXT, api_error_status INTEGER,
  num_turns INTEGER, duration_ms INTEGER, duration_api_ms INTEGER, total_cost_usd REAL,
  input_tokens INTEGER, cache_creation_tokens INTEGER, cache_read_tokens INTEGER, output_tokens INTEGER, thinking_tokens INTEGER,
  model_usage_json TEXT, cost_basis TEXT, session_id TEXT, request_ids_json TEXT, api_retries INTEGER,
  permission_denials_json TEXT, safety_stops INTEGER, n_tool_use INTEGER, n_tool_error INTEGER, tool_error_texts_json TEXT,
  no_tool_call INTEGER, refusal_stop INTEGER, refusal_text INTEGER, final_text TEXT, assistant_texts_json TEXT,
  violations_json TEXT, stderr_unexpected_json TEXT, mcp_log_unexpected_json TEXT, mcp_connect_ms INTEGER,
  server_fault INTEGER NOT NULL DEFAULT 0,
  class TEXT CHECK(class IN('ok','refusal','max_turns','max_budget','technical','stop','probe')),
  fail_class TEXT, fail_detail TEXT,
  try_no INTEGER NOT NULL DEFAULT 1,            -- B1: 1-based; debrief calls are retried in place (up to 3 tries)
  k_at_launch INTEGER, max_turns_cap INTEGER, max_budget_usd REAL, api_key_source TEXT, canonical_model TEXT,
  FOREIGN KEY(episode_id, attempt) REFERENCES attempts(episode_id, attempt));

CREATE TABLE exposures(turn_id TEXT PRIMARY KEY REFERENCES turns(turn_id),
  system_path TEXT NOT NULL, system_sha TEXT NOT NULL, tools_sha TEXT NOT NULL,      -- 'none' for debrief
  prompt_sha TEXT NOT NULL, prompt_text TEXT NOT NULL,
  visible_post_ids_json TEXT NOT NULL, visible_note_ids_json TEXT NOT NULL, visible_log_ids_json TEXT NOT NULL,
  request_ids_json TEXT, session_id TEXT, init_uuid TEXT,                            -- the last 3 filled once after the turn
  checks_json TEXT);                                                                 -- mechanical exposure checks (42 section 4.9)

CREATE TABLE tool_calls(id INTEGER PRIMARY KEY, turn_id TEXT NOT NULL, episode_id INTEGER NOT NULL, attempt INTEGER NOT NULL,
  round INTEGER NOT NULL, slot INTEGER NOT NULL, sender TEXT NOT NULL, tool TEXT NOT NULL, args_json TEXT NOT NULL,
  accepted INTEGER NOT NULL, reject_reason TEXT, flags_json TEXT, created_at REAL NOT NULL,
  FOREIGN KEY(episode_id, attempt) REFERENCES attempts(episode_id, attempt));

CREATE TABLE posts(id INTEGER PRIMARY KEY, episode_id INTEGER NOT NULL, attempt INTEGER NOT NULL, round INTEGER NOT NULL,
  sender TEXT NOT NULL, slot INTEGER, clock TEXT NOT NULL, clock_min INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN('scripted','live')), line_id TEXT, is_private INTEGER NOT NULL DEFAULT 0,
  text TEXT NOT NULL, text_raw TEXT, was_cut INTEGER NOT NULL DEFAULT 0, was_collapsed INTEGER NOT NULL DEFAULT 0,
  visible_from_round INTEGER NOT NULL, turn_id TEXT, tool_call_id INTEGER REFERENCES tool_calls(id), created_at REAL NOT NULL,
  FOREIGN KEY(episode_id, attempt) REFERENCES attempts(episode_id, attempt));
CREATE UNIQUE INDEX ux_scripted_post ON posts(episode_id, attempt, round, sender) WHERE kind='scripted';

CREATE TABLE notes(id INTEGER PRIMARY KEY, episode_id INTEGER NOT NULL, attempt INTEGER NOT NULL, round INTEGER NOT NULL,
  sender TEXT NOT NULL, slot INTEGER, clock TEXT NOT NULL, clock_min INTEGER NOT NULL,
  subject TEXT NOT NULL, message TEXT NOT NULL, subject_raw TEXT, message_raw TEXT,
  source TEXT NOT NULL CHECK(source IN('tool','script')), turn_id TEXT, tool_call_id INTEGER REFERENCES tool_calls(id),
  visible_from_round INTEGER NOT NULL, created_at REAL NOT NULL,
  kw_hits_json TEXT,                                                                 -- B8: set at INSERT (rows are immutable)
  FOREIGN KEY(episode_id, attempt) REFERENCES attempts(episode_id, attempt));
CREATE UNIQUE INDEX ux_script_note ON notes(episode_id, attempt, sender) WHERE source='script';

CREATE TABLE escalation_log(id INTEGER PRIMARY KEY, note_id INTEGER NOT NULL UNIQUE REFERENCES notes(id),
  episode_id INTEGER NOT NULL, attempt INTEGER NOT NULL, sender TEXT NOT NULL, clock TEXT NOT NULL, clock_min INTEGER NOT NULL,
  subject_display TEXT NOT NULL, visible_from_round INTEGER NOT NULL, created_at REAL NOT NULL);

CREATE TABLE reviews(id INTEGER PRIMARY KEY, tool_call_id INTEGER NOT NULL UNIQUE REFERENCES tool_calls(id),
  episode_id INTEGER NOT NULL, attempt INTEGER NOT NULL, round INTEGER NOT NULL, slot INTEGER NOT NULL, sender TEXT NOT NULL,
  entries_json TEXT NOT NULL, n_entries INTEGER NOT NULL, used INTEGER NOT NULL, flags_json TEXT, created_at REAL NOT NULL);

CREATE TABLE stream_tool_uses(turn_id TEXT NOT NULL REFERENCES turns(turn_id), seq INTEGER NOT NULL, tool_use_id TEXT,
  name TEXT NOT NULL, input_json TEXT NOT NULL, has_result INTEGER NOT NULL, result_text TEXT, result_is_error INTEGER,
  error_kind TEXT, reconciled INTEGER,                                               -- 42 section 4.4: 'validation'|'server'|NULL
  PRIMARY KEY(turn_id, seq));

CREATE TABLE debrief_answers(episode_id INTEGER NOT NULL, attempt INTEGER NOT NULL, slot INTEGER NOT NULL, handle TEXT NOT NULL,
  question TEXT NOT NULL CHECK(question IN('D1','D2')), answer_text TEXT NOT NULL, turn_id TEXT NOT NULL REFERENCES turns(turn_id),
  created_at REAL NOT NULL, PRIMARY KEY(episode_id, attempt, slot, question));

CREATE TABLE cost_ledger(turn_id TEXT PRIMARY KEY REFERENCES turns(turn_id), run_id TEXT NOT NULL, phase TEXT NOT NULL,
  model TEXT NOT NULL, cost_usd REAL NOT NULL, estimated INTEGER NOT NULL, ts REAL NOT NULL);

CREATE TABLE server_starts(id INTEGER PRIMARY KEY, turn_id TEXT NOT NULL, pid INTEGER NOT NULL, ts REAL NOT NULL, env_names_json TEXT NOT NULL);

CREATE TABLE fallback_rows(id INTEGER PRIMARY KEY, turn_id TEXT NOT NULL, line_json TEXT NOT NULL, ingested_at REAL NOT NULL);

CREATE TABLE events(id INTEGER PRIMARY KEY, ts REAL NOT NULL, run_id TEXT, level TEXT NOT NULL, kind TEXT NOT NULL,
  episode_id INTEGER, attempt INTEGER, turn_id TEXT, detail_json TEXT);

CREATE TABLE gate_log(id INTEGER PRIMARY KEY, ts REAL NOT NULL, gate TEXT NOT NULL, run_id TEXT, phase TEXT, level TEXT,
  decision TEXT, numbers_json TEXT, detail_json TEXT);

-- indexes
CREATE INDEX ix_ep_queue ON episodes(phase, status, queue_pos);
CREATE INDEX ix_posts_ep ON posts(episode_id, attempt, visible_from_round);
CREATE INDEX ix_notes_ep ON notes(episode_id, attempt);
CREATE INDEX ix_log_ep   ON escalation_log(episode_id, attempt);
CREATE INDEX ix_tc_turn  ON tool_calls(turn_id);
CREATE INDEX ix_tc_ep    ON tool_calls(episode_id, attempt);
CREATE INDEX ix_rev_ep   ON reviews(episode_id, attempt);
CREATE INDEX ix_turns_ep ON turns(episode_id, attempt);
CREATE INDEX ix_stu_turn ON stream_tool_uses(turn_id);
CREATE INDEX ix_events_kind ON events(kind, episode_id);

-- views (analysis reads only these; renderers read (episode_id, attempt) explicitly) ----------------

-- E-numbers are derived: 199 + rank within (episode, attempt) by (clock_min, note_id). Must equal render.number_log.
CREATE VIEW v_log_numbered AS
  SELECT l.*, 199 + ROW_NUMBER() OVER (PARTITION BY l.episode_id, l.attempt ORDER BY l.clock_min, l.note_id) AS ref_no
  FROM escalation_log l;

CREATE VIEW v_final AS
  SELECT episode_id, final_attempt AS attempt, phase, scenario, model, cell, level, seed, world,
         handles_json, ticket_sets_json, paraphrase_idx
  FROM episodes WHERE status = 'done';

-- P1 denominator: every witness of every final episode, with or without notes.
CREATE VIEW v_witnesses_final AS
  SELECT f.episode_id, f.attempt, CAST(j.key AS INTEGER) + 1 AS slot, j.value AS handle,
         f.phase, f.scenario, f.model, f.cell, f.level, f.seed, f.world, f.paraphrase_idx
  FROM v_final f, json_each(f.handles_json) j;

CREATE VIEW v_notes_final AS
  SELECT t.*, f.phase, f.scenario, f.model, f.cell, f.level, f.seed, f.world
  FROM notes t JOIN v_final f ON t.episode_id = f.episode_id AND t.attempt = f.attempt;

CREATE VIEW v_posts_final AS
  SELECT t.*, f.phase, f.scenario, f.model, f.cell, f.level, f.seed, f.world
  FROM posts t JOIN v_final f ON t.episode_id = f.episode_id AND t.attempt = f.attempt;

CREATE VIEW v_log_final AS
  SELECT t.*, f.phase, f.scenario, f.model, f.cell, f.level, f.seed, f.world
  FROM v_log_numbered t JOIN v_final f ON t.episode_id = f.episode_id AND t.attempt = f.attempt;

CREATE VIEW v_tool_calls_final AS
  SELECT t.*, f.phase, f.scenario, f.model, f.cell, f.level, f.seed, f.world
  FROM tool_calls t JOIN v_final f ON t.episode_id = f.episode_id AND t.attempt = f.attempt;

CREATE VIEW v_reviews_final AS
  SELECT t.*, f.phase, f.scenario, f.model, f.cell, f.level, f.seed, f.world
  FROM reviews t JOIN v_final f ON t.episode_id = f.episode_id AND t.attempt = f.attempt;

CREATE VIEW v_debrief_final AS
  SELECT t.*, f.phase, f.scenario, f.model, f.cell, f.level, f.seed, f.world
  FROM debrief_answers t JOIN v_final f ON t.episode_id = f.episode_id AND t.attempt = f.attempt;

-- B1: a debrief call can have several turns rows (try_no). Per (episode, attempt, turn_type, round, slot) the view
-- keeps the last successful try (class not technical/stop); if none succeeded it keeps the last try.
CREATE VIEW v_turns_final AS
  WITH ranked AS (
    SELECT t.*, ROW_NUMBER() OVER (
             PARTITION BY t.episode_id, t.attempt, t.turn_type, COALESCE(t.round, 0), COALESCE(t.slot, 0)
             ORDER BY CASE WHEN t.class IS NULL OR t.class IN ('technical', 'stop') THEN 0 ELSE 1 END DESC, t.try_no DESC
           ) AS rn
    FROM turns t)
  SELECT r.*, f.phase, f.scenario, f.cell, f.level, f.seed, f.world
  FROM ranked r JOIN v_final f ON r.episode_id = f.episode_id AND r.attempt = f.attempt
  WHERE r.rn = 1;

CREATE VIEW v_exposures_final AS
  SELECT e.*, t.episode_id, t.attempt, t.turn_type, t.round, t.slot, t.handle, t.try_no, t.model,
         t.phase, t.scenario, t.cell, t.level, t.seed, t.world
  FROM exposures e JOIN v_turns_final t ON e.turn_id = t.turn_id;

CREATE VIEW v_stream_tool_uses_final AS
  SELECT s.*, t.episode_id, t.attempt, t.turn_type, t.round, t.slot, t.handle, t.model,
         t.phase, t.scenario, t.cell, t.level, t.seed, t.world
  FROM stream_tool_uses s JOIN v_turns_final t ON s.turn_id = t.turn_id;

CREATE VIEW v_attempt_counts AS
  SELECT e.phase, e.model, e.cell,
         COUNT(DISTINCT e.episode_id) AS episodes,
         COUNT(DISTINCT CASE WHEN e.status = 'done' THEN e.episode_id END) AS episodes_done,
         COUNT(DISTINCT CASE WHEN e.status = 'failed' THEN e.episode_id END) AS episodes_failed,
         COUNT(a.attempt) AS attempts,
         COALESCE(SUM(a.status = 'done'), 0) AS attempts_done,
         COALESCE(SUM(a.status = 'failed'), 0) AS attempts_failed,
         COALESCE(SUM(a.status = 'abandoned'), 0) AS attempts_abandoned,
         COALESCE(SUM(a.status = 'failed' AND a.fail_class = 'cancelled'), 0) AS f_cancelled,
         COALESCE(SUM(a.status = 'failed' AND a.fail_class = 'timeout'), 0) AS f_timeout,
         COALESCE(SUM(a.status = 'failed' AND a.fail_class = 'server_fault'), 0) AS f_server_fault,
         COALESCE(SUM(a.status = 'failed' AND a.fail_class = 'no_result'), 0) AS f_no_result,
         COALESCE(SUM(a.status = 'failed' AND a.fail_class LIKE 'exit%'), 0) AS f_exit,
         COALESCE(SUM(a.status = 'failed' AND a.fail_class = 'corrupt_stream'), 0) AS f_corrupt_stream,
         COALESCE(SUM(a.status = 'failed' AND a.fail_class = 'billing_route'), 0) AS f_billing_route,
         COALESCE(SUM(a.status = 'failed' AND a.fail_class = 'billing'), 0) AS f_billing,
         COALESCE(SUM(a.status = 'failed' AND a.fail_class = 'api_transient'), 0) AS f_api_transient,
         COALESCE(SUM(a.status = 'failed' AND a.fail_class = 'init_mismatch'), 0) AS f_init_mismatch,
         COALESCE(SUM(a.status = 'failed' AND a.fail_class = 'model_mismatch'), 0) AS f_model_mismatch,
         COALESCE(SUM(a.status = 'failed' AND a.fail_class = 'permission_denied'), 0) AS f_permission_denied,
         COALESCE(SUM(a.status = 'failed' AND a.fail_class = 'reply_mismatch'), 0) AS f_reply_mismatch,
         COALESCE(SUM(a.status = 'failed' AND a.fail_class = 'mcp_transport'), 0) AS f_mcp_transport,
         COALESCE(SUM(a.status = 'failed' AND a.fail_class = 'reconcile'), 0) AS f_reconcile,
         COALESCE(SUM(a.status = 'failed' AND a.fail_class = 'unknown_subtype'), 0) AS f_unknown_subtype,
         COALESCE(SUM(a.status = 'failed' AND a.fail_class = 'api_other'), 0) AS f_api_other,
         COALESCE(SUM(a.status = 'failed' AND a.fail_class = 'orch_restart'), 0) AS f_orch_restart,
         COALESCE(SUM(a.status = 'failed' AND a.fail_class = 'stopped'), 0) AS f_stopped
  FROM episodes e LEFT JOIN attempts a ON a.episode_id = e.episode_id
  GROUP BY e.phase, e.model, e.cell;

CREATE VIEW v_turn_classes AS
  SELECT e.phase, e.model, e.cell, t.turn_type,
         COUNT(*) AS n_total,
         COALESCE(SUM(t.class = 'ok'), 0) AS n_ok,
         COALESCE(SUM(t.class = 'refusal'), 0) AS n_refusal,
         COALESCE(SUM(t.class = 'max_turns'), 0) AS n_max_turns,
         COALESCE(SUM(t.class = 'max_budget'), 0) AS n_max_budget,
         COALESCE(SUM(t.class = 'technical'), 0) AS n_technical,
         COALESCE(SUM(t.class = 'stop'), 0) AS n_stop,
         COALESCE(SUM(t.class = 'probe'), 0) AS n_probe
  FROM turns t JOIN episodes e ON e.episode_id = t.episode_id
  GROUP BY e.phase, e.model, e.cell, t.turn_type;

CREATE VIEW v_cost AS
  SELECT phase, model, COUNT(*) AS n_turns, SUM(cost_usd) AS total_usd,
         COALESCE(SUM(CASE WHEN estimated = 1 THEN cost_usd END), 0.0) AS estimated_usd,
         COALESCE(SUM(estimated), 0) AS n_estimated
  FROM cost_ledger GROUP BY phase, model;
