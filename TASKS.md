# Forge implementation tasks

## Phase 1: baseline verification (complete)

- [x] Verify baseline commit and source checkout changes.
- [x] Import only runtime source and requirements into the writable Forge workspace.
- [x] Exclude personal state, real configuration, credentials, datasets, and Git history.
- [x] Create development instructions and isolated Python 3.11 environment.
- [x] Add behavioral regression tests and retain the baseline failures (55 failed / 20 passed).
- [x] Fix confirmed OpenAI dispatch and rejected-result defects.
- [x] Fix confirmed Plan callback, plan-content, and mode-transition defects.
- [x] Fix model configuration propagation for subagents and Skill fork.
- [x] Replace the frontmatter parser with safe YAML and validate consumer fields.
- [x] Verify original configuration precedence using synthetic fixtures.
- [x] Lock dependencies, record imported file hashes, and write compatibility/report documents.
- [x] Run all phase-1 checks (81 passed) and document unresolved issues.

Evidence: `docs/phase1-report.md`, `docs/compatibility.md`, `docs/baseline.json`, `artifacts/phase1-*.xml`.
Known later-phase risks and publication-license prerequisites remain open; see the report.

## Decisions recorded on 2026-10-15

- [x] Distribution remote is the public repository `Charles-0219/forge`, under the MIT license.
- [x] Phase 5 live protocol is OpenAI-compatible `deepseek-flash`, thinking disabled, with a 15 USD cap for one full C-suite run.
- [x] Eval drafts S1–S8, Q1–Q20, D1–D12, and H1–H8 are approved and frozen under `evals/`.

## Phase 2: event kernel (complete)

- [x] Add in-memory and SQLite event stores behind one kernel.
- [x] Add the first Alembic migration for conversations, events, runs, actions, and approvals.
- [x] Adapt simulated OpenAI and Anthropic responses into paired actions.
- [x] Project recorded context without rereading disk or calling a model.
- [x] Keep the phase-1 agent runtime hashes and regressions green.

Evidence: `docs/phase2-report.md`.

## Phase 3: unified execution (complete)

- [x] Add policy checks that hard denies cannot be lifted by allow, yolo, or an approval.
- [x] Bind approvals to an action and its argument hash; repeat, conflict, and expiry stay distinct.
- [x] Execute the six built-in tools asynchronously and record structured results.
- [x] Add pause, cancel, terminal guards, crash recovery, plan approval, and fold.
- [x] Keep the phase-1 agent runtime hashes and the phase-2 kernel tests green.

Evidence: `docs/phase3-report.md`.

## Phase 4: child tasks, MCP, and isolation (complete)

- [x] Run child tasks through the phase-3 execution path, with serial fork-return, no nesting, and a shared root budget.
- [x] Remove the two child `bypassPermissions` constructions. explore, plan, and an empty allowlist cannot write or run shell.
- [x] Call only preconfigured stdio MCP tools through the dispatcher, and record timeout, disconnect, and protocol failures as observations.
- [x] Isolate skill, agent, permission, and memory caches by directory and config version.
- [x] Keep phases 1–3 green. `agents/agent.py` is the only agent file whose current hash differs from phase 1.

Evidence: `docs/phase4-report.md`. Phase 5 remains unauthorized.

## Later phases: not authorized in this task
- [ ] Phase 5: database Skill gate, isolated real evaluation, promotion and export.
- [ ] Phase 6: API, CLI migration, console and reproducible demonstrations.

Proceed only after explicit phase authorization and the preceding acceptance gate passes.
