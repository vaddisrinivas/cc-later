---
name: later
description: Queue out-of-scope follow-up work in .claude/LATER.md using a strict, dispatchable format.
---

# LATER Skill

Use `.claude/LATER.md` as a durable work queue for tasks that are valuable but out of scope for the current request. The cc-later plugin dispatches them automatically -- **one background agent per `##` section**, all running in parallel -- near the end of each Claude usage window.

Write to LATER.md when you encounter work that should happen but does not belong in the current response.

---

## When to write to LATER.md

**Write a LATER entry when you notice:**
- A bug or edge case adjacent to what you are fixing -- but not your current task
- A missing test for a code path you touched
- Documentation that is stale, absent, or misleading
- Technical debt or a code smell you spotted but were not asked to address
- A security or performance concern that needs follow-up
- Something the user explicitly deferred ("we'll fix that later", "track that")

**Do not write LATER entries for:**
- Work that is directly part of the current request -- just do it
- Highly speculative ideas with no clear action ("might want to rethink this someday")
- Tasks that require clarification the codebase cannot provide
- Anything that would take one line to fix right now

---

## Section design principles

Each `##` section becomes **one parallel background agent**. When cc-later dispatches, all sections run simultaneously. When `DISPATCH_ALLOW_FILE_WRITES=true`, each agent operates in its own isolated git worktree on a dedicated branch, so agents cannot conflict on files. When agents finish, their branches are merged back.

Design sections so that:

- Tasks within a section are **related to the same component, feature, or concern** -- an agent can complete them all without needing context from another section
- Sections are **independent** -- no section's tasks depend on another section's output
- **Sections do not overlap on files** -- when file writes are enabled, two sections editing the same file will cause a merge conflict on reconcile
- Section size is meaningful -- not so large an agent gets overwhelmed, not so small the overhead dominates

**Good groupings:**
- By module or layer: `## Auth`, `## Payments`, `## Reports`
- By file cluster: `## src/users`, `## src/api`
- By concern type that naturally isolates: `## Security`, `## Performance`

**Avoid:**
- A single large `## Queue` section with everything -- defeats parallelism
- Two sections that both touch the same source files -- risks merge conflict
- A `## Tests` section alongside a section that refactors the code under test -- the test file and source file are likely to conflict
- Sections that depend on each other's output (e.g. `## Scaffold` and `## Fill in scaffold` should be sequential, not parallel)

Tasks before the first `##` header are grouped into one unnamed agent. Always use explicit sections for new entries.

### When two tasks would conflict

If you need to add tests for code that another section is refactoring, either:
1. Put both tasks in the **same section** so one agent handles them in order
2. Leave the test task out of LATER.md entirely and add it to a new section after the refactor section is dispatched and merged

---

## Task writing rules

1. **Be specific and self-contained.** Agents have no chat context. Include file path, function name, class name, line number, or symptom -- whatever a developer would need to find and fix the issue cold.

2. **One issue per task.** Do not bundle multiple distinct fixes into one line.

3. **Write actions, not nouns.**
   - Good: `add rate limiting to POST /api/refresh in src/auth/api.py`
   - Bad: `rate limiting`

4. **Use correct priority.**
   - `P0` -- production issue, security vulnerability, data loss risk
   - `P1` -- standard follow-up, should be done reasonably soon
   - `P2` -- nice-to-have, cleanup, cosmetic

5. **Be concise.** One line. No trailing period. No preamble.

6. **Do not mark `[x]` yourself.** cc-later marks tasks done automatically based on structured agent output. Manual `[x]` entries are skipped by the parser.

---

## Complete format reference

```markdown
# LATER

## SectionName
- [ ] (P0) urgent task -- file/location hint
- [ ] (P1) normal task -- file/location hint
- [ ] (P2) low-priority task
- [!] shorthand for P0 urgent task
- [x] (P1) completed task (written by cc-later)
```

### Mark types

| Mark | Meaning | Who writes it |
|---|---|---|
| `[ ]` | Pending | You or cc-later capture |
| `[!]` | Pending, P0 priority (shorthand) | You or cc-later capture |
| `[x]` | Completed | cc-later (automatic) |

### Priority markers

| Marker | Priority | Use for |
|---|---|---|
| `(P0)` | Urgent | Production bugs, security issues, data loss |
| `(P1)` | Normal | Standard follow-up and improvements |
| `(P2)` | Low | Cleanup, cosmetic, speculative |
| _(absent)_ | Defaults to P1 | |

---

## Good and bad examples

### Good

```markdown
## Auth
- [ ] (P0) POST /api/refresh has no rate limiting -- brute-force possible (src/auth/api.py:88)
- [ ] (P1) Refresh token not invalidated on logout -- src/auth/service.py TokenService.logout()
- [ ] (P2) Dead parameter `legacy_mode` in TokenService.__init__ -- safe to remove

## Reports
- [ ] (P1) N+1 query in ReportGenerator.fetch_reports -- src/reports/service.py:fetch_reports()
- [ ] (P1) Add regression test for empty pagination cursor -- tests/test_reports.py
```

Two sections -> two parallel agents. Each agent has a clear, located task list.

### Bad

```markdown
## Misc
- [ ] auth stuff
- [ ] fix query
- [ ] tests
- [ ] cleanup
```

Fails because: one mega-section (no parallelism), vague descriptions (agent cannot act without searching), missing priorities on most entries, no file hints.

### Tasks that would conflict (avoid in same dispatch)

```markdown
## Auth
- [ ] (P1) refactor TokenService into two classes -- src/auth/service.py

## Tests
- [ ] (P1) add tests for TokenService -- tests/test_auth.py
```

If both agents edit `src/auth/service.py` and/or `tests/test_auth.py` simultaneously, a merge conflict is likely. Either combine them into one section, or sequence them across separate LATER.md cycles (do Auth first, then add Tests after the Auth branch is merged).

---

## Capture shortcut

If the user prompt contains a capture keyword, cc-later appends to LATER.md automatically -- you do not need to write the file.

Recognized patterns (case-insensitive):

```
later: <task text>
add to later: <task text>
note for later: <task text>
queue for later: <task text>
for later: <task text>
later[!]: <task text>    <-- urgent (P0)
```

Example:

```
That looks good. later: add integration test for the token refresh retry path
later[!]: the debug endpoint at /api/debug is unauthenticated
```

Captured tasks are appended without a section header (they go into the unnamed group). If you want them in a named section, write LATER.md directly.

---

## Interaction with dispatch

- Each section dispatches at most `LATER_MAX_ENTRIES_PER_DISPATCH` tasks per cycle (default: 3), selected in priority order.
- Tasks not selected this cycle remain pending and are eligible next cycle.
- Completed tasks are marked `[x]` and left in the file for history. They are invisible to the parser on future runs.
- If an agent hits a rate limit, its tasks are re-queued automatically and dispatched in the next fresh window.
- If an agent gets stuck (no output for `NUDGE_STALE_MINUTES`), it is killed and re-dispatched. If an agent crashes, it is re-queued. Max retries: `NUDGE_MAX_RETRIES` (default: 2).
- When `DISPATCH_ALLOW_FILE_WRITES=true`, each section agent runs on its own git branch (`cc-later/{section}-{timestamp}`) in an isolated worktree. Changes are merged back with `--no-ff` on completion. If a merge conflicts, the merge is aborted (repo stays clean), the worktree is preserved at `~/.cc-later/worktrees/`, and affected tasks are marked `NEEDS_HUMAN` for manual resolution.
- After `/compact` or auto-compaction, the SessionStart hook re-injects the LATER.md queue and window state into context so task awareness is preserved.
