
# Global project operating contract

Version: 1.0.0  
Owner: By JTT / project coordinator  
Applies to: every repository, game, web app, deployment, connector, and AI or human agent working on the project family.

This is a control contract, not a promise that tools will never fail. Its purpose is to make failure visible, preserve recoverability, and prevent an unverified state from being reported as complete. “Done” is an evidence state, not a tone of voice.

## Operating invariants

1. **One source of truth.** The repository ref and commit under review are the source. A games portal, Vercel URL, preview, compiled bundle, screenshot, chat transcript, or another repository is evidence at most; it is never a substitute for source.
2. **Fail closed.** If evidence is missing, the state remains blocked or partial. Do not infer success from a connector’s generic “Action completed” response, an HTTP 200 shell, a green-looking screenshot, a missing error, or an agent’s intention.
3. **Read after every write.** A mutating call is incomplete until the exact path, branch/ref, resulting commit, and intended content are read back. For deployment, read back the linked commit/ref and then exercise the public route.
4. **No silent loss.** Every failed tool call, unavailable browser, rate limit, missing artifact, cancelled agent, malformed response, and partial mutation gets an append-only failure record and a durable next action.
5. **No destructive recovery.** Never reset, force-push, delete, overwrite project instructions, or repoint production to an unverified source as a recovery shortcut. Use an isolated branch and additive changes.
6. **No unbounded concurrency.** One coordinator owns a run. Delegated scopes are disjoint. Shared files have one writer. If agent capacity is exhausted, the coordinator continues the safe critical path or checkpoints the exact gap; it never silently drops the workstream.
7. **No unverifiable release language.** Public status must distinguish `VERIFIED`, `PARTIAL`, `BLOCKED`, and `INFERRED`. A preview is not production. A deployment is not a release until it is linked to the intended source commit and passes smoke and browser checks.
8. **No unnecessary reinvention.** Before adding infrastructure, inspect existing project primitives, package ecosystem, supported framework, and recorded leverage notes. Add custom code only where it creates product value or closes a measured gap.
9. **No untracked provenance.** External assets, code, fonts, data, services, and licenses must be recorded before they become part of a shippable build. Unknown terms are a blocker.
10. **No evidence fabrication.** A test that did not run is not a passing test. A critic that could not inspect motion is not a motion approval. A provider with missing logs is not a clean CI run.

## Evidence state machine

Use these states in `.agents/session.json`, `.agents/handoff.json`, issue reports, and release notes:

| State | Meaning | Required evidence |
| --- | --- | --- |
| `DISCOVERED` | A project, artifact, or service was found | Identifier, location, and timestamp |
| `SOURCE_VERIFIED` | The intended repository/ref is readable | Commit SHA, relevant paths, and clean source readback |
| `CHANGED` | A bounded mutation was made | Branch, paths, resulting commit SHA |
| `LOCALLY_VERIFIED` | Deterministic checks pass | Exact commands, versions, and output summaries |
| `BROWSER_VERIFIED` | A real browser exercised the built app | Browser/version, URL, test/capture path, console/network result |
| `DEPLOYMENT_LINKED` | Hosting is connected to source | Provider project, branch, exact source commit/ref, build/output settings |
| `RELEASE_VERIFIED` | The public release is usable | All prior evidence plus HTTP/routes/assets/smoke/motion evidence |
| `BLOCKED` | A required gate cannot be run or failed | Failure ID, owner, safe next action |
| `PARTIAL` | Some gates passed, at least one required gate remains | Passed gates, missing gate, exact risk |

A state may only move forward with new evidence. Never move backward by overwriting history; append a transition.

## Standard run protocol

### Preflight

- Read repository `AGENTS.md`, `README.md`, nearest domain docs, `.agents/project.yaml`, and canonical handoff.
- Identify repository, default branch, current ref, source commit, deployment project, and active run ID.
- Refresh capabilities before invoking a connector, plugin, browser, or sub-agent. Record unavailable capabilities rather than assuming them.
- Inspect the working tree or remote file set before editing. Detect existing instructions and preserve them.
- Write a short task statement: user outcome, scope, acceptance evidence, risks, and rollback.

### Plan and delegate

- Assign each agent one bounded deliverable with an explicit input ref and output path.
- Do not give two agents write access to the same file or branch.
- Require a structured handoff containing: outcome, files, commit, checks, captures, performance, defects, and next action.
- Give critics authority to report defects, not to approve work by default.
- If agent creation fails or the platform reports a thread limit, record `DELEGATION_UNAVAILABLE`, continue safe coordinator work, and checkpoint the missing specialist review.

### Mutate

- Prefer branch + pull request for repository changes.
- Use exact current blob SHAs for file replacement.
- Make writes idempotent: a rerun detects the desired state and does not duplicate sections or create competing branches.
- Keep commits focused and conventional.
- Never mix unrelated project changes into a recovery or guardrail patch.

### Verify

Run the narrowest applicable complete gate set:

- dependency install from the tracked lockfile;
- typecheck/lint/unit tests;
- production build;
- deterministic source/schema/contract verification;
- a real browser run against the production build or deployment;
- visual and motion captures for interactive or game changes;
- performance measurements on the supported profile;
- security, accessibility, and license checks where relevant.

A browser gate that stops before launch is `NOT VERIFIED`. A CI run with missing steps, logs, or artifacts is `INFRA_UNVERIFIED`.

### Release

A deployment claim requires all of:

- hosting project identity;
- repository, branch, and exact source commit linkage;
- install/build/output settings;
- deployment URL and response;
- route, asset, and error smoke checks;
- browser evidence from the deployed URL;
- a recorded rollback target.

If the host cannot expose source linkage, keep the state `DEPLOYMENT_UNLINKED` and do not call the URL production.

### Handoff and continuation

At pause, failure, or completion update:

- `.agents/session.json`: run ID, phase, state, event cursor, last verified commit, next action, active leases;
- `.agents/handoff.json`: canonical project/release/quality status and residual risks;
- `docs/FAILURE_REGISTER.md`: new failure or near-miss, evidence, owner, mitigation, and re-test;
- `docs/agents/capability-matrix.json`: tool/provider evidence and freshness;
- the PR or commit body: checks, captures, defects, and release language.

A resumed agent starts from `next_action` and the last verified commit. It does not replay the conversation or assume a previous “working” statement is evidence.

## Failure classes and required behavior

| Class | Examples | Required state | Recovery |
| --- | --- | --- | --- |
| `SOURCE` | empty checkout, wrong repo, stale portal export | `BLOCKED` | locate recorded source surfaces; if explicitly authorized, rebuild on isolated branch and label provenance |
| `MUTATION` | wrong branch, stale SHA, partial write | `BLOCKED` | re-read current ref, stop competing writer, reconcile before retry |
| `VERIFY` | missing browser binary, skipped test, no capture | `PARTIAL` | install/use a supported browser or record an unverified gate |
| `PROVIDER` | `steps:null`, no logs/artifacts, rate limit, timeout | `INFRA_UNVERIFIED` | retry only when safe; preserve raw identifier and do not translate to pass |
| `DEPLOYMENT` | shell page, stale preview, missing source link, 404 | `NOT_RELEASED` | connect to verified source and run release checklist |
| `DELEGATION` | agent thread limit, cancellation, malformed handoff | `DELEGATION_UNAVAILABLE` | checkpoint scope and continue coordinator path |
| `SECURITY` | secret, unknown license, unsafe dependency, privacy gap | `BLOCKED` | quarantine and resolve before merge/release |
| `PRODUCT` | autopilot controls, weak loop, unreadable UX, accessibility defect | `PARTIAL` | file a player-facing issue with replay/capture evidence |

Failure records are never deleted when fixed. Mark them `FIXED` with the fix commit and re-test evidence.

## Cross-project adoption

A new project is not “bootstrapped” until it has, at minimum:

- `AGENTS.md` containing local source, test, and release rules plus a link to this contract;
- `.agents/project.yaml` or `.agents/project.json`;
- `.agents/session.json` and `.agents/handoff.json`;
- `docs/FAILURE_REGISTER.md`;
- a lockfile when the project has package dependencies;
- a CI or equivalent verification entry point;
- an explicit deployment source-of-truth record when it is hosted.

For existing repositories, adoption is additive. Preserve local instructions, licenses, scripts, architecture, and branch conventions. Add missing contract pieces only after reading the repo. Archived or empty repositories receive an audit record before any mutation; they are not mass-edited.

## Versioning and maintenance

- Review this contract when a failure class occurs, a provider changes behavior, a framework major version changes, or a new project type is introduced.
- Record the contract version in each project’s `.agents/project.yaml`.
- Dependency tooling may propose updates, but an update is not accepted without the applicable verification gates.
- Never promise permanent immunity. The durable guarantee is that known failure modes are detected, recorded, and prevented from being silently reported as success.
