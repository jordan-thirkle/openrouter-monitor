# Failure register

This is an append-only register. Missing tests, unavailable providers, stale deployments, malformed tool results, and agent handoff gaps must be recorded rather than converted into passes.

## F-BOOTSTRAP-001 — Cross-project contract adoption pending

- **Class:** GOVERNANCE
- **State:** OPEN / AUDIT_PENDING
- **Observed:** 2026-08-12 during cross-project inventory.
- **Impact:** This repository had no shared durable handoff/failure controls before this additive bootstrap.
- **Mitigation:** Added the shared contract, project manifest, session state, handoff, and contract workflow.
- **Next action:** Run project-specific source, build, browser, accessibility, performance, security, licensing, and deployment checks; append evidence here.
- **Release rule:** This record does not certify the project or its deployment.
