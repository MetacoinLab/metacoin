#!/usr/bin/env bash
# Order 08 §74: live upgrade of the owner's running service (tmux session metacoin-service, windows api/worker, port 8402).
# Backup with keys -> isolated restore check -> drain and stop task-owned processes by recorded pid -> migrate -> restart in
# the same tmux windows -> verify (health/loaded revision, worker, old artifact read, new document op, generation, optimization,
# loopback binding, credentials preserved without printing them). Never touches the Claude session's tmux, never kills by name.
set -u
LIVE="$HOME/.local/state/metacoin-service"; OUT="$1"; REPO="$HOME/projects/metacoin"; PY="$REPO/.venv-service/bin/python"; PORT=8402
mkdir -p "$OUT"; cd "$REPO" || exit 1
CAND="$(git rev-parse HEAD)"; TS="$(date +%s)"
log() { echo "[$(date -u +%FT%TZ)] $*" | tee -a "$OUT/live-upgrade.log"; }
TOK="$(python3 -c "import json; print(json.load(open('$LIVE/credentials/bootstrap.json'))['principals']['owner']['token'])")"
H=(-H "Authorization: Bearer $TOK")
log "candidate $CAND"; log "before: $(curl -s http://127.0.0.1:$PORT/api/health)"
BEFORE_SCHEMA="$(sqlite3 "$LIVE/service.sqlite" 'SELECT name FROM schema_migrations ORDER BY name DESC LIMIT 1')"; log "schema before $BEFORE_SCHEMA"
# 1. backup (SQLite backup API, WAL-consistent) including keys, then prove it opens in an isolated restore
BK="$HOME/.local/state/metacoin-service-backups/pre-migration-032-$TS"
PYTHONPATH="$REPO" "$PY" -m metacoin_service --home "$LIVE" --provider-mode test-http backup "$BK" --include-keys > "$OUT/backup-manifest.json" 2>>"$OUT/live-upgrade.log" || { log "BACKUP FAILED"; exit 2; }
log "backup at $BK: $(python3 -c "import json; d=json.load(open('$OUT/backup-manifest.json')); print(d.get('contains',{}).get('artifacts'), 'schema', d.get('schema_migrations',[''])[-1] if isinstance(d.get('schema_migrations'), list) else d.get('schema_migrations'))")"
RESTORED="$(mktemp -d)/restored-home"
PYTHONPATH="$REPO" "$PY" -m metacoin_service --home "$RESTORED" --provider-mode test-http restore "$BK" --keys-dir "$BK/keys" > "$OUT/restore-check.json" 2>>"$OUT/live-upgrade.log" || { log "RESTORE CHECK FAILED"; exit 2; }
PYTHONPATH="$REPO" "$PY" -m metacoin_service --home "$RESTORED" --provider-mode test-http migrate > "$OUT/restore-migrate.json" 2>>"$OUT/live-upgrade.log"
PYTHONPATH="$REPO" "$PY" -m metacoin_service --home "$RESTORED" --provider-mode test-http status > "$OUT/restore-status.json" 2>>"$OUT/live-upgrade.log" && log "isolated restore opened and migrated: $(python3 -c "import json; print(json.load(open('$OUT/restore-status.json')).get('schema',[''])[-1])")"
rm -rf "$(dirname "$RESTORED")"
# 2. drain the worker, wait for in-flight work, stop task-owned processes by the pids recorded in tmux panes
tmux set-option -t metacoin-service remain-on-exit on 2>>"$OUT/live-upgrade.log" || log "tmux session metacoin-service missing before stop (will be recreated)"   # a killed pane must not close its window (and the session with it)
API_PID="$(tmux list-panes -s -t metacoin-service -F '#{pane_pid} #{window_name}' | awk '$2=="api"{print $1}')"; WRK_PID="$(tmux list-panes -s -t metacoin-service -F '#{pane_pid} #{window_name}' | awk '$2=="worker"{print $1}')"
API_CHILD="$(pgrep -P "$API_PID" | head -1)"; WRK_CHILD="$(pgrep -P "$WRK_PID" | head -1)"
log "pane pids api=$API_PID (child $API_CHILD) worker=$WRK_PID (child $WRK_CHILD)"
WID="$(curl -s "${H[@]}" http://127.0.0.1:$PORT/api/v1/status | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('workers',{}))")"; log "workers: $WID"
for w in $(sqlite3 "$LIVE/service.sqlite" "SELECT id FROM workers WHERE state='active'"); do curl -s -o /dev/null -w "drain $w %{http_code}\n" -X POST "${H[@]}" "http://127.0.0.1:$PORT/api/v1/workers/$w/drain" | tee -a "$OUT/live-upgrade.log"; done
for i in $(seq 1 60); do R="$(sqlite3 "$LIVE/service.sqlite" "SELECT COUNT(*) FROM jobs WHERE state='running'")"; [ "$R" = "0" ] && break; sleep 2; done; log "running jobs after drain: $R; queued: $(sqlite3 "$LIVE/service.sqlite" "SELECT COUNT(*) FROM jobs WHERE state='queued'")"
for p in ${WRK_CHILD:-} ${API_CHILD:-} $WRK_PID $API_PID; do [ -n "$p" ] && ps -p "$p" -o args= | grep -q "metacoin_service" && { kill -TERM "$p"; log "TERM $p"; }; done   # pane pids included: after an exec respawn the pane process IS the service
for i in $(seq 1 30); do alive=0; for p in ${WRK_CHILD:-} ${API_CHILD:-}; do [ -n "$p" ] && ps -p "$p" >/dev/null 2>&1 && alive=1; done; [ $alive = 0 ] && break; sleep 1; done; log "old processes stopped (alive=$alive)"
# 3. migrate
PYTHONPATH="$REPO" "$PY" -m metacoin_service --home "$LIVE" --provider-mode test-http migrate > "$OUT/migrate.json" 2>>"$OUT/live-upgrade.log" || { log "MIGRATE FAILED: restore from $BK"; exit 3; }
log "migrated: $(cat "$OUT/migrate.json" | head -c 300)"
# 4. restart in the same tmux windows (task-owned), record pids; recreate the session if the windows closed
tmux has-session -t metacoin-service 2>/dev/null || { tmux new-session -d -s metacoin-service -n api "sleep 1"; tmux set-option -t metacoin-service remain-on-exit on; tmux new-window -d -t metacoin-service -n worker "sleep 1"; log "tmux session metacoin-service recreated (api, worker windows)"; }
tmux respawn-window -k -t metacoin-service:api "cd $REPO && exec $PY -m metacoin_service --home $LIVE --provider-mode test-http serve --port $PORT" 2>>"$OUT/live-upgrade.log" || tmux new-window -t metacoin-service -n api "cd $REPO && exec $PY -m metacoin_service --home $LIVE --provider-mode test-http serve --port $PORT"
for i in $(seq 1 60); do curl -s -m 2 http://127.0.0.1:$PORT/api/health >/dev/null 2>&1 && break; sleep 1; done
tmux respawn-window -k -t metacoin-service:worker "cd $REPO && exec $PY -m metacoin_service --home $LIVE --provider-mode test-http worker --name live-worker" 2>>"$OUT/live-upgrade.log" || tmux new-window -t metacoin-service -n worker "cd $REPO && exec $PY -m metacoin_service --home $LIVE --provider-mode test-http worker --name live-worker"
sleep 8
tmux list-panes -s -t metacoin-service -F '#{pane_pid} #{window_name}' | tee -a "$OUT/live-upgrade.log"
# 5. verify
curl -s http://127.0.0.1:$PORT/api/health | tee "$OUT/live-health.json" | tee -a "$OUT/live-upgrade.log"; echo
LOADED="$(python3 -c "import json; print(json.load(open('$OUT/live-health.json'))['revision'])")"; [ "$LOADED" = "$CAND" ] && log "loaded revision matches candidate" || log "LOADED REVISION MISMATCH $LOADED"
ss -ltnp 2>/dev/null | grep ":$PORT " | tee -a "$OUT/live-upgrade.log"
curl -s "${H[@]}" http://127.0.0.1:$PORT/api/v1/status > "$OUT/live-status.json"; curl -s "${H[@]}" http://127.0.0.1:$PORT/api/v1/capabilities > "$OUT/live-capabilities.json"
python3 -c "import json; d=json.load(open('$OUT/live-status.json')); print('workers', d['workers'], 'documents', d.get('documents'), 'package_runs', d.get('package_runs_by_state'))" | tee -a "$OUT/live-upgrade.log"
OLDART="$(sqlite3 "$LIVE/service.sqlite" "SELECT id FROM artifacts WHERE deleted_at IS NULL AND workspace='ws_default' AND created_at < $TS ORDER BY created_at LIMIT 1")"
curl -s -o /dev/null -w "old artifact $OLDART export %{http_code}\n" "${H[@]}" "http://127.0.0.1:$PORT/api/v1/artifacts/$OLDART/export" | tee -a "$OUT/live-upgrade.log"
CID="$(curl -s -X POST "${H[@]}" -H 'Content-Type: application/json' -d '{"name":"post-upgrade smoke","description":"synthetic"}' http://127.0.0.1:$PORT/api/v1/knowledge/collections | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")"
DOC="$(curl -s -X POST "${H[@]}" -H 'Content-Type: application/pdf' --data-binary @metacoin_service/tests/document_fixtures/dev/report.pdf "http://127.0.0.1:$PORT/api/v1/documents/import?name=smoke-report.pdf&collection_id=$CID" | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")"
GEN="$(curl -s -X POST "${H[@]}" -H 'Content-Type: application/json' -d '{"inputs":{"messages":[{"role":"user","content":"Reply with one word: upgraded"}],"max_output_tokens":8}}' http://127.0.0.1:$PORT/api/v1/models/generate | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('job_id') or d)")"
PLAN="$(python3 -c "import json,sys; sys.path.insert(0,'.'); from metacoin_service.tests.test_resource_plan_service import sample; print(json.dumps({'inputs': dict(sample(), private_label='LIVE_SMOKE')}))" | curl -s -X POST "${H[@]}" -H 'Content-Type: application/json' -d @- http://127.0.0.1:$PORT/api/v1/compute/resource-plans | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('job_id') or d)")"
# new contract path: draft + inspect work terms (nothing reserved), and the reconciliation view
WT="$(curl -s -X POST "${H[@]}" -H 'Content-Type: application/json' -d '{"template":"determination","ceiling":1}' http://127.0.0.1:$PORT/api/v1/work/terms | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('id') or d)")"
curl -s "${H[@]}" http://127.0.0.1:$PORT/api/v1/work/terms/$WT/inspect > "$OUT/live-work-terms-inspect.json"; log "work terms drafted $WT (inspect written)"
curl -s -X POST "${H[@]}" -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:$PORT/api/v1/work/journal/replay > "$OUT/live-journal-replay.json"; log "journal replay: $(python3 -c "import json; d=json.load(open('$OUT/live-journal-replay.json')); print('consistent', d.get('consistent'))")"
log "smoke: document $DOC generation $GEN plan $PLAN"
for i in $(seq 1 120); do D="$(curl -s "${H[@]}" http://127.0.0.1:$PORT/api/v1/documents/$DOC | python3 -c "import json,sys; print(json.load(sys.stdin).get('state'))")"; G="$(curl -s "${H[@]}" http://127.0.0.1:$PORT/api/v1/jobs/$GEN | python3 -c "import json,sys; print(json.load(sys.stdin).get('state'))")"; P="$(curl -s "${H[@]}" http://127.0.0.1:$PORT/api/v1/jobs/$PLAN | python3 -c "import json,sys; print(json.load(sys.stdin).get('state'))")"; [ "$D" = ready ] && [ "$G" = succeeded ] && [ "$P" = succeeded ] && break; sleep 3; done
log "smoke states: document=$D generation=$G plan=$P (outcome $(curl -s "${H[@]}" http://127.0.0.1:$PORT/api/v1/jobs/$PLAN | python3 -c "import json,sys; print(json.load(sys.stdin).get('outcome'))"))"
python3 - <<PYEOF > "$OUT/live-upgrade.json"
import json, time
print(json.dumps({'candidate': '$CAND', 'loaded_revision': '$LOADED', 'schema_before': '$BEFORE_SCHEMA', 'schema_after': open('$OUT/migrate.json').read()[:300], 'backup': '$BK', 'backup_includes_keys': True, 'smoke': {'document': '$DOC', 'document_state': '$D', 'generation': '$GEN', 'generation_state': '$G', 'plan': '$PLAN', 'plan_state': '$P'}, 'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}, indent=1))
PYEOF
log "done"
