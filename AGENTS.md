# openrouter-monitor agent contract

This repository follows the shared fail-closed project operating contract in `docs/agents/global-project-operating-contract.md`. Read it before changing source, invoking tools, or making deployment claims.

## Source of truth

This repository at a verified branch and commit is the source of truth. Inspect the real project files and toolchain before selecting commands. A portal, preview URL, generated bundle, screenshot, or another repository is not source.

## Verification

Record exact commands and results in the handoff. Tests that did not run are `NOT VERIFIED`; provider results without steps/logs/artifacts are `INFRA_UNVERIFIED`. Read every remote write back by path, ref, and commit.

## Shared fail-closed operating contract

This repository follows `docs/agents/global-project-operating-contract.md`.

- The repository at a verified ref/commit is the only source of truth; portals, previews, bundles, screenshots, and other repos are not source.
- Missing evidence remains `PARTIAL` or `BLOCKED`; never infer completion from a generic tool response, HTTP 200 shell, skipped test, or missing log.
- Read every remote write back by exact path/ref and resulting commit. Deployment requires source linkage, route/asset smoke, and browser evidence.
- `steps:null`, missing logs/artifacts, rate limits, unavailable browsers, and agent-capacity limits are `INFRA_UNVERIFIED` / `DELEGATION_UNAVAILABLE`, never green.
- One coordinator owns the run; delegated scopes are disjoint; resumable state lives in `.agents/session.json` and `.agents/handoff.json`.
- Existing instructions, licenses, and architecture are preserved; bootstrap is additive and non-destructive.
