# Locally registered exploratory protocol

## Objective and scope

Demonstrate a controller that preserves an explicit mission and verifiable outputs when declared execution resources change. The system under control is a closed operator DAG and its worker processes. Registered work is file hashing, digest verification, one manifest, and an optional `llm:grok` chat receipt. The controller maintains validated plans, estimates, observations, goal checks and revision lineage. OODA is the organizing loop; completion and correctness are evaluated through explicit predicates.

Prior exposure is disclosed: the implementation followed discussion of LLM operators, local ATFT findings, an existing documentary coherence pilot, and public harness designs. This is an exploratory local registration. It is not a prospective preregistration of previously observed ATFT findings and makes no claim of blind analysis or independent human review.

## Observe

Collect mission/resource records and immutable input identities. For public-release fixtures, collect only bounded filesystem metadata through `inspect`; retain truncation and non-atomic snapshot caveats. Historical source notes and design references are distinct from runtime observations. Refuse malformed records, altered inputs, unsafe paths and unsupported operations.

## Orient

Represent requirements and acceptance criteria separately from unresolved questions. Preserve uncertainty about intent, acceptance, authority and tradeoffs. The source registry records exact public commit identities where available; mutable JPL web content has a collection record and a release-authored interpretation-note hash. No hidden source revision or empirical cost estimate is inferred.

Distinguish controller state from worker state. An estimate of a task's duration is not its measured elapsed time. A read-buffer reservation is not RSS. A worker slot is not a physical CPU core. A deterministic digest check is not a general scientific validity check. A filtered worker environment is not an operating-system sandbox, and a recorded environment name is not an external measurement of the child process. A model-budget reservation is not an invoice, a market rate, or a local token count. A provider-reported usage field is not a measurement made by this controller. The nanosecond cost model is not that budget.

## Decide

Construct a graph with declared dependencies, state reads, state writes, output ownership and registered operator implementations. Bind candidates that fit the declared resource profile. Use an estimated-completion scheduling heuristic; state its estimate origin and avoid any global-optimality claim. Reject cycles, producerless state reads, conflicting writers, missing verification paths and a manifest that omits inputs.

Admit candidates that fit the declared read buffer and, when a model budget is present, the per-task worst-case reservation. Construct schedules with descending remaining critical-path duration, then task ID. Keep a start only when a worker slot, the aggregate read-buffer reservation, and the running sum of model reservations all fit. Select by estimated makespan, then lower total estimated work, then candidate identifier. This secondary work criterion avoids choosing a slower implementation merely because a reference task dominates completion time. An earlier local demonstration exposed that tie; its original evidence is not distributed.

The default planner is `coordinate-list-v2`. It does not enumerate every candidate combination. It searches from three fixed seeds by one- and two-candidate changes, for at most eight improving passes per seed. A seed keeps a move when it is strictly better than that seed's current key. The recorded plan is the best key among the walks, and a later seed may step through a key worse than an earlier seed. That walk can keep a valid binding whose list-schedule key is worse than the binding full enumeration would select, when the improvement needs three or more simultaneous changes or a longer chain of improvements. For the registered candidates, model reservations do not depend on the `sha256` candidate and every remaining candidate fits the read buffer alone, so a mission v1 can plan is still planned by v2. A future candidate that broke either fact could leave an unvisited vector as the only feasible one. Plans record the planner id and version. A plan with no planner stamp is replayed with the earlier enumerator, which accepts at most 64 nodes. Generate fresh public-release evidence with `demo`. The scaling test bounds one 2048-node plan by 20 seconds in-process. That bound is not a speedup claim.

Freeze each plan by hashing the mission, resource profile, graph, bindings, estimates, source snapshot and parent lineage. Source changes require a new plan. Replanning creates a new record; it does not rewrite an earlier plan or run.

## Act and verify

Only own Python worker subprocesses execute the registered operations. The controller does not pass its process environment through. Each worker receives the platform start allowlist, two forced interpreter flags, and environment names the resource profile grants to that task. On Windows those names are matched without regard to case and recorded in uppercase. Evidence records those names and omits values. An unset grant fails that task before launch. Retain observations, task outputs, exit statuses, timeouts, errors, dispatch events and declared reservations. Failed dependencies block downstream work. A successful worker status must agree with its actual return code and validated output schema. Kill and reap timed-out workers through the executor's own process handles.

Compare primary and reference SHA-256 values with each other and the mission input identity. Assemble a manifest only from successful verifications. An `llm:grok` task either posts one non-streaming Chat Completions request to `https://api.x.ai/v1/chat/completions` or reads one declared fixture file. The API key reaches only a task granted `XAI_API_KEY`. The receipt stores hashes, provider-reported token counts, latency, and HTTP status. Assistant text is an evidence artifact addressed by hash. The receipt does not score that text. When the resource profile declares a model budget, reserve the worst case for each task before launch and do not start a task whose hold would exceed a remaining cap. After a receipt that fits the hold, replace the hold with the reported usage and the profile price. A launched task without a fitting receipt keeps the hold. Record reserved and actual amounts and the run totals. Check goal predicates, buffer reservations, and that budget ledger against retained events. Replay recomputes the ledger offline and rejects a mismatch. It does not call the network or fabricate omitted observations or omitted usage.

## Fixed demonstration

Use three files: zero bytes; UTF-8 `abc`; a deterministic 131,073-byte repeating pattern. Baseline: two worker slots, 4,194,304 read-buffer bytes, 60 seconds per task. Revision: 65,536 read-buffer bytes, with the same mission. Cost origin: `injected_fixture`; actual execution times are measured separately. That revision changes a read-buffer cap. It does not declare a model budget.

A separate injected profile can declare a model budget and a per-model price table. Tightening that budget is another resource revision. A cap that still covers every worst-case reservation produces a new plan whose recorded limits and remaining headroom differ. A cap one unit below a reservation produces no execution plan. The prices in the checked-in model profiles are fixtures for a 4-byte prompt and a 16-token output bound. They are not a provider rate.

Accept digest agreement, bounded reservation accounting, explicit lineage, replayable records, and preservation of existing research. A revision can be slower or inconclusive. Never convert an injected estimate into an observed speedup or aggregate resource-preservation claim.

## Extension gates

A remote chat receipt is now a registered operator under the bounds in CONTRACT.md. That registration does not activate local weights, a forward pass, a sampler, a trainer, a task scorer, or the geometry checks below. Prompt and fixture files still pass through the hash and verification path. Replay checks archived hashes and does not open a socket. Numerical tolerances beyond the declared temperature grid, timeout, and byte caps are not claimed. Strict causality and operator identity for later instruments still require separate gates. Trained operators and post-hoc analysis instruments need separate contracts.

For geometry experiments, test rank-deficient Procrustes completions, paired-prompt conventions, held-out transport, dimension and basis sensitivity, metric definitions and causal interventions. A projected fitted linear map is not automatically the full nonlinear Jacobian. Spectral similarity alone does not establish semantic truth, sentience, hidden actors, universal mechanisms, or deployment savings.

## Reporting

Report exact tested scope, estimated-versus-observed quantities, failure outcomes and unresolved questions. Preserve negative and null results. Human review remains pending until a person actually records it. No inter-reviewer reliability is reported without a second independent human review.
