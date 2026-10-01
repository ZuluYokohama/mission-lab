# Known assurance gaps — v0.1 public preview

This controller is experimental. Its existing tests and fixture checks do not establish complete V&V, authenticity of records supplied by an adversary, or fitness for consequential deployment.

1. Plan reconstruction shares production planning logic. Some resealed incorrect parent/revision lineage is accepted: the checker does not independently compare the revision with an archived parent.
2. Observation checking does not independently authenticate recorded worker command, PID or duration against an external process observer. Some resealed contradictory metadata is accepted. The controller's own event ledger is not an independent sensor.
3. Some plan numeric fields accept Python-equal Boolean/float substitutions after resealing. Worker execution of those altered plans has not been established.
4. Replay does not yet cross-check every raw job, raw output, staged file, semantic result and event through a separate implementation. Hashes detect alteration relative to a retained manifest; they do not establish authorship or authenticity.
5. Exceptional worker wait/kill paths need stronger cleanup and evidence-finalization checks. Dependency-blocked and aborted states need independent state-machine review.
6. Primary and reference paths share the standard-library SHA-256 provider. External known vectors and a separate provider are needed for stronger independence.
7. Injected duration estimates do not model calibrated end-to-end process startup and verification overhead. No measured completion advantage is claimed.
8. Human review, independent reproduction and intended-use validation remain pending. Replay is not a new execution. Reserved buffers are not total process memory, CPU cores or energy.

Use disposable owned fixtures. Do not use v0.1 as a sandbox for hostile code, a proof of scheduler optimality, or a source of scientific claims beyond its recorded checks. The planned response is in VV_PLAN.md; that campaign has not yet been implemented.
