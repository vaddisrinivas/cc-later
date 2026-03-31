---
name: later
description: Append out-of-scope but worthwhile follow-up work to .claude/LATER.md.
---

# LATER Skill

Use this skill to keep a lightweight backlog in `.claude/LATER.md`.

## Behavior
1. When you notice work that is valuable but outside the current user request, append one actionable line to `.claude/LATER.md`.
2. Use `- [ ]` for normal priority.
3. Use `- [!]` only for urgent issues: security, data loss, or production-impacting bugs.
4. Never auto-run tasks from `LATER.md`; the handler owns dispatch.
5. Never rewrite old entries unless explicitly asked.

## Guidance
- Keep each entry single-line and specific.
- Prefer concrete nouns and file/function hints.
- Do not add speculative tasks with no evidence.

## Example
```markdown
- [ ] Add rate limiting to /refresh endpoint in auth middleware
- [!] Fix SQL injection risk in report filter query builder
```
