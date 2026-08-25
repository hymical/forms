# Claude Code Instructions

Read `AGENTS.md` before making changes to this repository.

`AGENTS.md` is the canonical source for project architecture, security
invariants, repository conventions, testing requirements, and working rules.
Do not duplicate it here and do not treat this file as a second copy of it.

Also read the relevant MkDocs pages under `docs/` before changing behavior in
an area they document, so a change and its documentation do not drift apart.

If `.context/context.md` exists, read it for current task context. Treat it as
temporary working context, not as authoritative project documentation: it may
be stale, and it is not a substitute for reading the code.

When instructions conflict, current user instructions take precedence over
temporary context. Do not silently override an established invariant in
`AGENTS.md` because of something written in `.context/context.md`; surface the
conflict instead.
