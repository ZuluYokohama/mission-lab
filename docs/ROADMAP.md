# Roadmap

The immediate goal is a controller whose recorded claims can be inspected and challenged. The existing fixture demonstration establishes a bounded functional result; it does not complete verification and validation. Current observations are in [VALIDATION.md](../VALIDATION.md), and unresolved findings are in [KNOWN_ISSUES.md](../KNOWN_ISSUES.md).

## Next: independent V&V

Implement the separately proposed [V&V campaign](../VV_PLAN.md) against a frozen, reviewed contract:

- Check records, resource intervals, execution states and revision lineage through an implementation that does not import the production checker.
- Use external digest vectors and another provider; demonstrate detection of deliberately seeded faults.
- Exercise worker failure and cleanup paths, preserve partial evidence, and compare controller observations with an external observer.
- Calibrate cost estimates and compare equivalent execution baselines before making performance claims.
- Have a human validate whether the requirements and outcomes serve the intended use. Record unresolved questions and null findings.

Each change needs a scoped issue, acceptance rule and reviewable evidence. Completing repository automation alone does not complete this work.

## Later, subject to separate specifications

General numerical operators, precision policies, CPU/GPU/NPU execution, archive reanalysis, model runtimes and `llama.cpp` integration are deferred. Device discovery does not establish workload support or measured capacity. Future hardware contracts must distinguish available resources, enforced reservations and actual observations.

Software generation and broader mission types also require new operation contracts and boundary tests. No delivery date or performance outcome is promised for these extensions.
