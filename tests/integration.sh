#!/usr/bin/env bash
# tests/integration.sh — cc-later end-to-end integration tests
#
# Tests three layers:
#   1. capture.py   — key-phrase detection via piped JSON (mimics UserPromptSubmit hook)
#   2. handler.py   — gate logic and --dry-run mode (via CC_LATER_APP_DIR override)
#   3. Full dispatch — real `claude -p` round-trip: LATER.md in → result out → [x] marked
#
# Usage:
#   bash tests/integration.sh                          # run all sections (full dispatch)
#   bash tests/integration.sh --no-dispatch            # skip section 3 (avoids live claude call)
#   bash tests/integration.sh --scan-dir ~/Projects    # also run section 4 against real repos
#   bash tests/integration.sh --no-dispatch --scan-dir ~/Projects
#
# Requirements: python3 (>=3.11), git, claude CLI in PATH

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HANDLER="$REPO_ROOT/scripts/handler.py"
CAPTURE="$REPO_ROOT/scripts/capture.py"
STATUS="$REPO_ROOT/scripts/status.py"

RUN_DISPATCH=true
SCAN_DIR=""
_PREV=""
for arg in "$@"; do
  if [[ "$arg" == "--no-dispatch" ]]; then
    RUN_DISPATCH=false
  elif [[ "$_PREV" == "--scan-dir" ]]; then
    SCAN_DIR="${arg/#\~/$HOME}"
  fi
  _PREV="$arg"
done
unset _PREV

# ── Colours ─────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'
PASS=0; FAIL=0

pass() { echo -e "${GREEN}✓${NC} $1"; PASS=$((PASS + 1)); }
fail() { echo -e "${RED}✗${NC} $1"; FAIL=$((FAIL + 1)); }
skip() { echo -e "${YELLOW}–${NC} $1 (skipped)"; }
section() { echo -e "\n${BOLD}=== $1 ===${NC}"; }

# ── Temp dir management ──────────────────────────────────────────────────────
TMP_ROOTS=()
make_tmp() { local d; d=$(mktemp -d); TMP_ROOTS+=("$d"); echo "$d"; }
cleanup() {
  for d in "${TMP_ROOTS[@]:-}"; do [[ -d "$d" ]] && rm -rf "$d" || true; done
}
trap cleanup EXIT

# ── Helpers ──────────────────────────────────────────────────────────────────
make_git_repo() {
  local dir="$1"
  git -C "$dir" init -q
  git -C "$dir" config user.email "test@test.local"
  git -C "$dir" config user.name "Test"
  git -C "$dir" commit --allow-empty -q -m "init"
}

make_cc_later_dir() {
  # Creates a minimal ~/.cc-later equivalent with the given config
  local app_dir="$1"; local watch_path="$2"; local extra="${3:-}"
  mkdir -p "$app_dir"
  cat > "$app_dir/config.toml" << TOML
[paths]
watch = ["$watch_path"]

[dispatch]
enabled = true
model = "sonnet"
allow_file_writes = false

[window]
dispatch_mode = "always"
idle_grace_period_minutes = 0
$extra
TOML
}

capture_json() {
  # Pipe a JSON payload to capture.py from the given directory
  local dir="$1"; local prompt="$2"
  (cd "$dir" && echo "{\"prompt\":$(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$prompt")}" \
    | python3 "$CAPTURE" 2>&1)
}

# ════════════════════════════════════════════════════════════════════════════
section "1. capture.py — key phrase detection"
# ════════════════════════════════════════════════════════════════════════════

REPO1=$(make_tmp)
make_git_repo "$REPO1"

# 1.1 basic later: phrase creates LATER.md and appends entry
capture_json "$REPO1" "later: fix the auth bug" > /dev/null
if grep -q "fix the auth bug" "$REPO1/.claude/LATER.md" 2>/dev/null; then
  pass "1.1  later: phrase creates LATER.md and appends entry"
else
  fail "1.1  later: phrase creates LATER.md — content: $(cat "$REPO1/.claude/LATER.md" 2>/dev/null || echo '<missing>')"
fi

# 1.2 priority flag
capture_json "$REPO1" "later[!]: SQL injection in filter builder" > /dev/null
if grep -q "\[!\] SQL injection" "$REPO1/.claude/LATER.md"; then
  pass "1.2  later[!]: sets [!] priority marker"
else
  fail "1.2  later[!]: sets [!] priority marker"
fi

# 1.3 add to later:
capture_json "$REPO1" "add to later: update the README install steps" > /dev/null
if grep -q "update the README install steps" "$REPO1/.claude/LATER.md"; then
  pass "1.3  add to later: appends entry"
else
  fail "1.3  add to later: appends entry"
fi

# 1.4 note for later:
capture_json "$REPO1" "note for later: UserService.delete() swallows exceptions" > /dev/null
if grep -q "UserService.delete" "$REPO1/.claude/LATER.md"; then
  pass "1.4  note for later: appends entry"
else
  fail "1.4  note for later: appends entry"
fi

# 1.5 deduplication — repeat 1.1, count should not increase
BEFORE=$(wc -l < "$REPO1/.claude/LATER.md")
capture_json "$REPO1" "later: fix the auth bug" > /dev/null
AFTER=$(wc -l < "$REPO1/.claude/LATER.md")
if [[ "$BEFORE" -eq "$AFTER" ]]; then
  pass "1.5  duplicate entry is not appended"
else
  fail "1.5  duplicate entry is not appended (before=$BEFORE after=$AFTER)"
fi

# 1.6 bare "later" without colon must NOT trigger
BEFORE=$(wc -l < "$REPO1/.claude/LATER.md")
capture_json "$REPO1" "I will handle this later when I have time" > /dev/null
AFTER=$(wc -l < "$REPO1/.claude/LATER.md")
if [[ "$BEFORE" -eq "$AFTER" ]]; then
  pass "1.6  bare 'later' without colon does not trigger"
else
  fail "1.6  bare 'later' without colon does not trigger (before=$BEFORE after=$AFTER)"
fi

# 1.7 empty prompt is a no-op
(cd "$REPO1" && echo '{"prompt":""}' | python3 "$CAPTURE" > /dev/null)
pass "1.7  empty prompt exits 0 without error"

# 1.8 non-matching prompt does not create LATER.md in fresh repo
REPO2=$(make_tmp)
make_git_repo "$REPO2"
(cd "$REPO2" && echo '{"prompt":"just a regular question"}' | python3 "$CAPTURE" > /dev/null)
if [[ ! -f "$REPO2/.claude/LATER.md" ]]; then
  pass "1.8  non-matching prompt does not create LATER.md"
else
  fail "1.8  non-matching prompt does not create LATER.md"
fi

# ════════════════════════════════════════════════════════════════════════════
section "2. handler.py — gate logic and --dry-run"
# ════════════════════════════════════════════════════════════════════════════

REPO3=$(make_tmp)
make_git_repo "$REPO3"
APP3=$(make_tmp)
make_cc_later_dir "$APP3" "$REPO3"

# Seed LATER.md with a couple of tasks
mkdir -p "$REPO3/.claude"
cat > "$REPO3/.claude/LATER.md" << 'MD'
# LATER
- [!] Fix critical bug in auth module
- [ ] Add missing type hints to utils.py
- [x] Remove dead import
MD

# 2.1 --dry-run exits 0
if echo "" | CC_LATER_APP_DIR="$APP3" python3 "$HANDLER" --dry-run > /tmp/cc_later_dry_run.txt 2>&1; then
  pass "2.1  --dry-run exits 0"
else
  fail "2.1  --dry-run exits 0 (exit code $?)"
fi

# 2.2 --dry-run prints header
if grep -q "dry-run" /tmp/cc_later_dry_run.txt; then
  pass "2.2  --dry-run output contains 'dry-run' header"
else
  fail "2.2  --dry-run output contains 'dry-run' header — got: $(cat /tmp/cc_later_dry_run.txt)"
fi

# 2.3 --dry-run shows gate checks
if grep -qE "[✓✗]" /tmp/cc_later_dry_run.txt; then
  pass "2.3  --dry-run shows gate check symbols"
else
  fail "2.3  --dry-run shows gate check symbols — got: $(cat /tmp/cc_later_dry_run.txt)"
fi

# 2.4 --dry-run shows LATER.md entries
if grep -q "Fix critical bug\|Add missing type hints" /tmp/cc_later_dry_run.txt; then
  pass "2.4  --dry-run lists pending LATER.md entries"
else
  fail "2.4  --dry-run lists pending LATER.md entries — got: $(cat /tmp/cc_later_dry_run.txt)"
fi

# 2.5 handler with dispatch disabled logs skip and exits 0
APP_DISABLED=$(make_tmp)
make_cc_later_dir "$APP_DISABLED" "$REPO3"
# Override: disable dispatch
sed -i.bak 's/enabled = true/enabled = false/' "$APP_DISABLED/config.toml"
OUT=$(echo "" | CC_LATER_APP_DIR="$APP_DISABLED" python3 "$HANDLER" 2>&1 || true)
if echo "$OUT" | grep -q -i "disabled\|Dispatch disabled"; then
  pass "2.5  disabled config prints informative message"
else
  fail "2.5  disabled config prints informative message — got: $OUT"
fi

# 2.6 status.py exits 0 and shows required sections
STATUS_OUT=$(CC_LATER_APP_DIR="$APP3" python3 "$STATUS" 2>&1)
STATUS_EXIT=$?
if [[ $STATUS_EXIT -eq 0 ]]; then
  pass "2.6  status.py exits 0"
else
  fail "2.6  status.py exits 0 (got $STATUS_EXIT)"
fi
for section_name in "Window" "Gates" "Queue" "Recent Runs"; do
  if echo "$STATUS_OUT" | grep -q "$section_name"; then
    pass "2.7  status.py output contains '### $section_name'"
  else
    fail "2.7  status.py output contains '### $section_name' — got: $(echo "$STATUS_OUT" | head -20)"
  fi
done

# ════════════════════════════════════════════════════════════════════════════
section "3. Full claude -p dispatch round-trip"
# ════════════════════════════════════════════════════════════════════════════

if ! $RUN_DISPATCH; then
  skip "3.x  Full dispatch (pass --no-dispatch to enable skipping, remove flag to run)"
  echo -e "      Re-run without --no-dispatch to test full dispatch round-trip."
else
  REPO4=$(make_tmp)
  make_git_repo "$REPO4"
  APP4=$(make_tmp)
  make_cc_later_dir "$APP4" "$REPO4"

  # Seed with a trivially-answerable task
  mkdir -p "$REPO4/.claude"
  cat > "$REPO4/.claude/LATER.md" << 'MD'
# LATER
- [ ] Print the string INTEGRATION_TEST_OK to stdout and nothing else
MD

  echo "  Spawning handler (this will call claude -p, may take 30-90s)..."
  DISPATCH_OUT=$(echo "" | CC_LATER_APP_DIR="$APP4" python3 "$HANDLER" 2>&1 || true)

  if echo "$DISPATCH_OUT" | grep -qi "dispatching\|dispatch\|spawn\|Dispatched"; then
    pass "3.1  handler logged a dispatch event"
  else
    fail "3.1  handler logged a dispatch event — output: $DISPATCH_OUT"
  fi

  # Wait for the result file to appear (up to 120s)
  RESULT_DIR="$APP4"
  DEADLINE=$((SECONDS + 120))
  RESULT_FILE=""
  echo "  Waiting for dispatch result..."
  while [[ $SECONDS -lt $DEADLINE ]]; do
    RESULT_FILE=$(find "$RESULT_DIR" -name "*.txt" -newer "$REPO4/.claude/LATER.md" 2>/dev/null | head -1 || true)
    [[ -n "$RESULT_FILE" ]] && break
    sleep 3
  done

  if [[ -n "$RESULT_FILE" ]] && [[ -f "$RESULT_FILE" ]]; then
    pass "3.2  result file written: $(basename "$RESULT_FILE")"
  else
    fail "3.2  result file written (timed out after 120s — check $RESULT_DIR)"
  fi

  # Run handler again to reconcile (mark [x] in LATER.md)
  if [[ -n "$RESULT_FILE" ]]; then
    echo "" | CC_LATER_APP_DIR="$APP4" python3 "$HANDLER" > /tmp/cc_later_reconcile.txt 2>&1 || true
    if grep -q "\[x\]" "$REPO4/.claude/LATER.md" 2>/dev/null; then
      pass "3.3  completed entry marked [x] in LATER.md after reconcile"
    else
      fail "3.3  completed entry marked [x] in LATER.md — content: $(cat "$REPO4/.claude/LATER.md")"
    fi
  else
    skip "3.3  mark [x] reconcile (result file not available)"
  fi

  # Verify run_log.jsonl captured events
  if [[ -f "$APP4/run_log.jsonl" ]]; then
    pass "3.4  run_log.jsonl created"
    if grep -q '"dispatch"' "$APP4/run_log.jsonl"; then
      pass "3.5  dispatch event logged in run_log.jsonl"
    else
      fail "3.5  dispatch event logged — log: $(cat "$APP4/run_log.jsonl")"
    fi
  else
    fail "3.4  run_log.jsonl created"
    skip "3.5  dispatch event in run_log.jsonl"
  fi
fi

# ════════════════════════════════════════════════════════════════════════════
section "4. Real-repo scan (--scan-dir)"
# ════════════════════════════════════════════════════════════════════════════

if [[ -z "$SCAN_DIR" ]]; then
  skip "4.x  Real-repo scan (use --scan-dir ~/Projects to enable)"
else
  # Collect up to 10 git repos from the scan dir (direct children only)
  mapfile -t REAL_REPOS < <(
    find "$SCAN_DIR" -mindepth 1 -maxdepth 1 -type d \
      -exec test -d "{}/.git" \; -print 2>/dev/null \
    | sort | head -10
  )

  if [[ ${#REAL_REPOS[@]} -eq 0 ]]; then
    skip "4.0  No git repos found in $SCAN_DIR"
  else
    echo "  Found ${#REAL_REPOS[@]} repos in $SCAN_DIR"

    APP_SCAN=$(make_tmp)
    # Build watch list as TOML array
    WATCH_LIST=""
    for r in "${REAL_REPOS[@]}"; do
      WATCH_LIST+="  \"$r\",\n"
    done
    mkdir -p "$APP_SCAN"
    printf '[paths]\nwatch = [\n%b]\n\n[dispatch]\nenabled = true\nmodel = "sonnet"\nallow_file_writes = false\n\n[window]\ndispatch_mode = "always"\nidle_grace_period_minutes = 0\n' \
      "$WATCH_LIST" > "$APP_SCAN/config.toml"

    IDX=1
    for REAL_REPO in "${REAL_REPOS[@]}"; do
      REPO_NAME="$(basename "$REAL_REPO")"

      # 4.A capture.py: simulate a "later:" prompt in each repo
      TASK_TEXT="integration test entry for $REPO_NAME at $(date +%s)"
      CAPTURE_OUT=$(cd "$REAL_REPO" && echo "{\"prompt\":\"later: $TASK_TEXT\"}" \
        | python3 "$CAPTURE" 2>&1 || true)
      LATER_FILE="$REAL_REPO/.claude/LATER.md"
      if grep -qF "$TASK_TEXT" "$LATER_FILE" 2>/dev/null; then
        pass "4.$IDX.A  [$REPO_NAME] capture.py appended entry to LATER.md"
      else
        fail "4.$IDX.A  [$REPO_NAME] capture.py appended entry — output: $CAPTURE_OUT"
      fi

      # Remove the test entry we just added (leave repo clean)
      if [[ -f "$LATER_FILE" ]]; then
        # Use python3 for portable in-place edit
        python3 - "$LATER_FILE" "$TASK_TEXT" << 'PYEOF'
import sys
path, text = sys.argv[1], sys.argv[2]
lines = open(path).readlines()
lines = [l for l in lines if text not in l]
open(path, 'w').writelines(lines)
PYEOF
      fi

      # 4.B handler --dry-run against each repo's real state
      DRY_OUT=$(echo "" | CC_LATER_APP_DIR="$APP_SCAN" python3 "$HANDLER" --dry-run 2>&1 || true)
      if echo "$DRY_OUT" | grep -q "dry-run\|Gate\|✓\|✗"; then
        pass "4.$IDX.B  [$REPO_NAME] --dry-run produces gate output"
      else
        fail "4.$IDX.B  [$REPO_NAME] --dry-run produces gate output — got: $(echo "$DRY_OUT" | head -5)"
      fi

      IDX=$((IDX + 1))
    done

    # 4.Z status.py with the real scan config
    STATUS_SCAN=$(CC_LATER_APP_DIR="$APP_SCAN" python3 "$STATUS" 2>&1 || true)
    if echo "$STATUS_SCAN" | grep -q "Queue"; then
      pass "4.Z  status.py shows Queue section for scanned repos"
    else
      fail "4.Z  status.py shows Queue section — got: $(echo "$STATUS_SCAN" | head -10)"
    fi
  fi
fi

# ════════════════════════════════════════════════════════════════════════════
echo
echo -e "${BOLD}Results: ${GREEN}${PASS} passed${NC}  ${RED}${FAIL} failed${NC}"
if [[ $FAIL -eq 0 ]]; then exit 0; else exit 1; fi
