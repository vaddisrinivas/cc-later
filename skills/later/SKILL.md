---
name: later
description: Append out-of-scope but worthwhile follow-up work to .claude/LATER.md.
---

# LATER Skill

cc-later runs tasks from `.claude/LATER.md` as headless `claude -p` background jobs when your session window is near expiry. This skill makes you a good writer of those tasks — entries that a future, context-free Claude agent can actually execute.

---

## Core rule

When you notice work that is valuable but outside the current request, append it to `.claude/LATER.md` in the active repo. Don't ask — just append and mention it briefly: _"I've noted X for later."_

The one exception: if acting on something right now would save significant user effort (e.g. a one-line fix you spotted), offer the choice first.

---

## When to append

Append when you notice any of the following while doing your primary task:

**Code health signals**
- A function or module clearly missing error handling, null checks, or boundary guards
- An obvious N+1 query, missing index, or performance hotspot backed by code evidence
- A TODO/FIXME/HACK comment that has a clear, bounded fix
- Dead code, unreachable branches, or stale feature flags
- Type annotations absent from a public API surface

**Documentation drift**
- README steps that no longer match the actual CLI flags, config keys, or file layout
- A public function or class with no docstring
- A CHANGELOG that's behind the most recent commits

**Dependency signals**
- An import of a deprecated API (visible from comments or known patterns)
- A pinned version that blocks an upgrade (only file when you see a concrete conflict)

**Test gaps**
- A non-trivial code path with no corresponding test (back it with file evidence)
- A test that always passes because it's asserting the wrong thing

**Security signals** (always `[!]`)
- SQL/command/path injection risk in user-controlled input
- Secrets or credentials hardcoded or logged
- Auth bypass conditions visible in the code

Only append when you have direct code evidence. Do not speculate.

---

## How to write a good entry

The background agent has no session context. Write every entry as if handing it to a fresh Claude that knows only the repo contents.

**Formula:** `[Verb] [specific target] in [file or module] — [one-line why]`

| Part | What it does |
|------|-------------|
| Verb | Sets the action: Audit, Fix, Add, Update, Check, Remove |
| Specific target | Names the function, class, endpoint, or config key |
| Location hint | File path or module name so the agent navigates directly |
| Why | One clause explaining the evidence or expected outcome |

**Strong entries**
```markdown
- [ ] Fix missing error handling in `UserService.delete()` (src/services/user.py) — silently swallows DB exceptions
- [ ] Add type hints to all public methods in `utils/http.py` — currently all `Any`
- [ ] Update README install steps to match current `pyproject.toml` — still references `setup.py`
- [ ] Remove dead `legacy_auth` branch in `middleware/auth.py:58` — feature flag removed in v2
- [!] Fix SQL injection in `ReportFilter.build_query()` (src/reports/filter.py) — user input concatenated directly
```

**Weak entries** (don't write these)
```markdown
- [ ] Improve error handling                    ← no target, no location
- [ ] Refactor the auth module                  ← vague, unbounded
- [ ] Maybe look at the database queries        ← speculative ("maybe")
- [ ] Fix all the TODOs                         ← too broad
- [!] Security issue                            ← no detail for the agent to act on
```

---

## Priority markers

**`- [ ]`** — Default. Code health, docs, deps, test gaps, refactors.

**`- [!]`** — Reserved for: security vulnerabilities, data loss risks, production-impacting bugs. Use sparingly. When in doubt, use `[ ]`.

**`- [x]`** — Done. Set by the handler automatically; do not set manually.

---

## Constraints the background agent operates under

Before filing, mentally check: can a headless `claude -p` do this with only the repo?

| ✓ File it | ✗ Don't file it |
|-----------|----------------|
| Read and report on code | Requires credentials or env secrets |
| Edit specific files (if `allow_file_writes` is on) | Requires running the app, tests, or build |
| Parse and summarize docs | Requires approval, deployment, or human sign-off |
| Fix a bounded, locatable bug | Requires interactive clarification |
| Check README against code | Requires access to external services or APIs |

If a task requires the user's judgment to complete, note it in conversation instead of LATER.md.

---

## Volume discipline

- **Max 3 new entries per session.** Quality over quantity — a LATER.md that fills with noise gets ignored.
- **Check for duplicates** before appending. Scan existing entries; don't re-file something already there.
- **Don't batch-file everything you see.** Pick the highest-value items. If you notice 10 things, file the 3 most impactful.

---

## File location and format

```
<repo-root>/.claude/LATER.md
```

If the file doesn't exist, create it with this header:

```markdown
# LATER
```

Append new entries at the bottom. Never reorder, edit, or delete existing entries unless explicitly asked.

---

## Full example

```markdown
# LATER

- [!] Fix SQL injection in `ReportFilter.build_query()` (src/reports/filter.py) — user input concatenated directly into raw SQL
- [ ] Add missing error handling in `UserService.delete()` (src/services/user.py) — DB exceptions silently swallowed
- [ ] Update README install steps — still references `setup.py`, project uses `pyproject.toml` since v1.2
- [x] Remove unused `legacy_auth` import in middleware/auth.py
```
