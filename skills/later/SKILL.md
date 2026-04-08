---
name: later
description: Queue out-of-scope follow-up work in .claude/LATER.md using a strict, dispatchable format. Parallel agents execute tasks near window end.
---

# LATER Skill

Use `.claude/LATER.md` as a durable work queue for tasks that are valuable but out of scope for the current request. The cc-later plugin dispatches them automatically — **one background agent per `##` section**, all running in parallel — near the end of each Claude usage window.

---

## When to write to LATER.md

**Write a LATER entry when you notice:**
- A bug or edge case adjacent to what you are fixing — but not your current task
- A missing test for a code path you touched
- Documentation that is stale, absent, or misleading
- Technical debt or a code smell you spotted but were not asked to address
- A security or performance concern that needs follow-up
- Something the user explicitly deferred ("we'll fix that later", "track that")

**Do NOT write LATER entries for:**
- Work that is directly part of the current request — just do it
- Highly speculative ideas with no clear action
- Tasks that require clarification the codebase cannot provide
- Anything that would take one line to fix right now

---

## Capture shortcut (fastest way)

Instead of editing LATER.md directly, include a capture keyword in your response or the user can include it in their prompt:

```
later: add integration test for the token refresh retry path
later[!]: the debug endpoint at /api/debug is unauthenticated
add to later: update readme with new config options
```

| Pattern | Priority |
|---|---|
| `later: <text>` | P1 (normal) |
| `later[!]: <text>` | P0 (urgent) |
| `add to later: <text>` | P1 |
| `note for later: <text>` | P1 |
| `queue for later: <text>` | P1 |
| `for later: <text>` | P1 |

Captured tasks are appended without a section header. For named sections, write LATER.md directly.

---

## Section design

Each `##` section becomes **one parallel background agent**. Design sections so agents can work independently.

### Rules

1. **Group by component** — tasks within a section should relate to the same module, feature, or concern
2. **Keep sections independent** — no section's tasks should depend on another section's output
3. **Avoid file overlap** — when file writes are enabled, two sections editing the same file will cause a merge conflict
4. **Right-size sections** — not so large an agent gets overwhelmed, not so small the overhead dominates

### Good groupings

```markdown
## Auth
- [ ] (P0) fix token refresh in src/auth/service.py
- [ ] (P1) add rate limiting to POST /api/refresh

## Reports
- [ ] (P1) N+1 query in ReportGenerator.fetch_reports
- [ ] (P1) add regression test for empty pagination cursor
```

Two sections = two parallel agents. Each has clear, located tasks.

### What to avoid

```markdown
## Misc
- [ ] auth stuff
- [ ] fix query
```

One mega-section (no parallelism), vague descriptions (agent cannot act), no file hints.

### When tasks would conflict

If you need to add tests for code that another section is refactoring:
1. Put both tasks in the **same section** so one agent handles them in order, OR
2. Leave the test task out and add it after the refactor is merged

---

## Task format

```markdown
# LATER

## SectionName
- [ ] (P0) urgent task — file/location hint
- [ ] (P1) normal task — file/location hint
- [ ] (P2) low-priority task
- [!] shorthand for P0 urgent task
- [x] (P1) completed task (written by cc-later)
```

### Priority

| Marker | Use for |
|---|---|
| `(P0)` | Production bugs, security issues, data loss |
| `(P1)` | Standard follow-up and improvements (default) |
| `(P2)` | Cleanup, cosmetic, speculative |

### Task writing rules

1. **Be specific and self-contained.** Include file path, function name, or line number. Agents have no chat context.
2. **One issue per task.** Do not bundle multiple distinct fixes.
3. **Write actions, not nouns.** "add rate limiting to POST /api/refresh in src/auth/api.py" not "rate limiting"
4. **Be concise.** One line. No trailing period.
5. **Do not mark `[x]` yourself.** cc-later marks tasks done automatically.

---

## How dispatch works

- Each section dispatches at most `LATER_MAX_ENTRIES_PER_DISPATCH` tasks per cycle (default: 3), selected in priority order (P0 first)
- Tasks not selected this cycle remain pending for next cycle
- Completed tasks are marked `[x]` automatically
- If an agent hits a rate limit, tasks are re-queued for the next fresh window
- If an agent gets stuck (no output for 10m), it is killed and re-dispatched (max 2 retries)
- When `DISPATCH_ALLOW_FILE_WRITES=true`, each agent runs on its own git branch in an isolated worktree. Changes merge back with `--no-ff`. Conflicts preserve the worktree for manual resolution.
- After `/compact`, the SessionStart hook re-injects the queue and window state into context

### Check status

```
/cc-later:status
```

Shows window state, queue, gate status, and recent runs.
