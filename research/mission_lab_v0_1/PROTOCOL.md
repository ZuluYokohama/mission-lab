# Locally registered exploratory protocol

## Objective and scope

Demonstrate a controller that preserves an explicit mission and verifiable outputs when declared execution resources change. The system under control is a closed file-hashing operator DAG and its worker processes. The controller maintains validated plans, estimates, observations, goal checks and revision lineage. OODA is the organizing loop; completion and correctness are evaluated through explicit predicates.

Prior exposure is disclosed: the implementation followed discussion of LLM operators, local ATFT findings, an existing documentary coherence pilot, and public harness designs. This is an exploratory local registration. It is not a prospective preregistration of previously observed ATFT findings and makes no claim of blind analysis or independent human review.

## Observe

Collect mission/resource records and immutable input identities. For public-release fixtures, collect only bounded filesystem metadata through `inspect`; retain truncation and non-atomic snapshot caveats. Historical source notes and design references are distinct from runtime observations. Refuse malformed records, altered inputs, unsafe paths and unsupported operations.

## Orient

Represent requirements and acceptance criteria separately from unresolved questions. Preserve uncertainty about intent, acceptance, authority and tradeoffs. The source registry records exact public commit identities where available; mutable JPL web content has a collection record and a release-authored interpretation-note hash. No hidden source revision or empirical cost estimate is inferred.

Distinguish controller state from worker state. An estimate of a task's duration is not its measured elapsed time. A read-buffer reservation is not RSS. A worker slot is not a physical CPU core. A deterministic digest check is not a general scientific validity check.

## Decide

Construct a graph with declared dependencies, state reads, state writes, output ownership and registered operator implementations. Bind candidates that fit the declared resource profile. Use an estimated-completion scheduling heuristic; state its estimate origin and avoid any global-optimality claim. Reject cycles, producerless state reads, conflicting writers, missing verification paths and a manifest that omits inputs.

Enumerate feasible candidate bindings and construct schedules using descending remaining critical-path duration, then task ID. Select by estimated makespan, then lower total estimated work, then candidate identifier. This secondary work criterion avoids choosing a slower implementation merely because a reference task dominates completion time. An earlier local demonstration exposed that tie; its original evidence is not distributed. Generate fresh public-release evidence with `demo`.

Freeze each plan by hashing the mission, resource profile, graph, bindings, estimates, source snapshot and parent lineage. Source changes require a new plan. Replanning creates a new record; it does not rewrite an earlier plan or run.

## Act and verify

Only own Python worker subprocesses execute the registered operations. Retain observations, task outputs, exit statuses, timeouts, errors, dispatch events and declared reservations. Failed dependencies block downstream work. A successful worker status must agree with its actual return code and validated output schema. Kill and reap timed-out workers through the executor's own process handles.

Compare primary and reference SHA-256 values with each other and the mission input identity. Assemble a manifest only from successful verifications. Check goal predicates and resource reservations against retained events. Replay reconstructs recorded conclusions offline and detects altered evidence. It does not rerun a model or fabricate omitted observations.

## Fixed demonstration

Use three files: zero bytes; UTF-8 `abc`; a deterministic 131,073-byte repeating pattern. Baseline: two worker slots, 4,194,304 read-buffer bytes, 60 seconds per task. Revision: 65,536 read-buffer bytes, with the same mission. Cost origin: `injected_fixture`; actual execution times are measured separately.

Accept digest agreement, bounded reservation accounting, explicit lineage, replayable records, and preservation of existing research. A revision can be slower or inconclusive. Never convert an injected estimate into an observed speedup or aggregate resource-preservation claim.

## Extension gates

Before activating later LLM work, establish artifact lineage, source/operator version, permissions, compatible execution environment, numerical tolerances, task scorer and cost boundary. Strict causality and operator identity require separate gates. Trained operators and post-hoc analysis instruments need separate contracts.

For geometry experiments, test rank-deficient Procrustes completions, paired-prompt conventions, held-out transport, dimension and basis sensitivity, metric definitions and causal interventions. A projected fitted linear map is not automatically the full nonlinear Jacobian. Spectral similarity alone does not establish semantic truth, sentience, hidden actors, universal mechanisms, or deployment savings.

## Reporting

Report exact tested scope, estimated-versus-observed quantities, failure outcomes and unresolved questions. Preserve negative and null results. Human review remains pending until a person actually records it. No inter-reviewer reliability is reported without a second independent human review.
