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

## Later phases: not authorized in this task

- [ ] Phase 2: adapters, event store, protocol projector, SQLite persistence.
- [ ] Phase 3: unified policy/dispatch, approvals, async execution, lifecycle and recovery.
- [ ] Phase 4: MCP/subagent/Memory/config isolation and shared root budgets.
- [ ] Phase 5: database Skill gate, isolated real evaluation, promotion and export.
- [ ] Phase 6: API, CLI migration, console and reproducible demonstrations.

Proceed only after explicit phase authorization and the preceding acceptance gate passes.
