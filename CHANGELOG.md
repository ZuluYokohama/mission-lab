# Changelog

Changes here describe the repository, not completion of the independent V&V campaign.

## Unreleased

- Plan closed graphs of up to 2048 nodes. Inputs remain 1 to 8. Select bindings with planner `coordinate-list-v2` instead of the Cartesian product, and record that planner id and version on the plan. Replay of a plan with no planner stamp still uses the previous enumerator, limited to 64 nodes. A test plans one 2048-node synthetic graph with a 20-second in-process limit. This entry does not state a speedup factor.
- Keep a coordinate move when it is strictly better than that seed's current assignment. The recorded plan is the best key among the walks. A later seed may step through a key that is worse than an earlier seed. This can change `enumerated_bindings`, and therefore a version-2 `plan_id`, even when the bindings stay the same. The selected key is equal to or better than the key that walk recorded before this comparison. Planner v1 is unchanged. This entry does not state a speedup.
- Add an optional model budget on the resource profile. Caps cover requests, spend in a profile-supplied fixed-point unit, and, when declared, output tokens and total tokens. Prices are integers per token or per million tokens and are not built in. Planning reserves a worst case from the input-token bound and `max_output_tokens` and refuses a plan that cannot hold every task. The scheduler refuses a launch that would exceed the remaining cap. A fitting receipt settles the hold to provider-reported usage; a launched task without one keeps the hold. A launch failure after the process exists keeps that worst-case hold charged, and replay rejects a ledger that releases it. A failure before the process starts does not charge it. Reports and replay record reserved and actual amounts. A profile that omits the budget keeps the previous reservation behavior.
- Add a registered `llm:grok` operator. A task declares a model id, an inline prompt or a confined prompt file, and bounds on output tokens, temperature, timeout, and response bytes. Transport `xai` calls `https://api.x.ai/v1/chat/completions` with urllib and a granted `XAI_API_KEY`. Transport `fixture` reads a declared file and does not use the network. Receipts store hashes, reported token usage, latency, and HTTP status, and never the key. Response text is a hash-addressed evidence artifact. Replay checks archived bytes and does not call the network.
- Skip graph compilation for an unresolved mission that has no `worker_env`. An unresolved mission that declares grants is still compiled so an unknown grant fails closed, and it still returns `needs_clarification`.
- Render retained task errors with sorted keys so a report regenerated from canonical JSON matches the report written at execution.
- Reject an observation or launch event whose environment names differ, including an allowed platform name present on only one of those records. Replay a launch that failed because a granted variable was unset.
- Stop registered workers from inheriting the controller process environment. Launch them with a platform start allowlist, two forced interpreter flags, and per-task resource grants. Reject undeclared grant names at validation. Record environment names, not values, on launch events and observations.
- On Windows, match worker environment names without regard to case and record them in uppercase, so a case variant cannot grant a reserved name or copy a variable that was not granted.
- Add contributor, security, development, review and roadmap documentation.
- Define repository checks, Windows and Ubuntu test jobs, and advisory CodeRabbit review configuration. Hosted checks and integration availability require verification after publication.
- Preserve the existing package layout, recorded validation and local evidence boundaries.

## 0.1.0 preview — prepared locally

- Add registered SHA-256 and manifest operations, bounded planning, worker execution, evidence replay and linked resource revisions.
- Include deterministic fixtures and a baseline versus tighter-buffer demonstration.
- Publish scoped validation results in [VALIDATION.md](VALIDATION.md), with unresolved assurance gaps in [KNOWN_ISSUES.md](KNOWN_ISSUES.md).
- Prepare a clean public derivative under PolyForm Noncommercial 1.0.0. Private research studies, archive inventories, model artifacts and original run evidence are excluded.

No remote publication date or release tag is asserted here.
