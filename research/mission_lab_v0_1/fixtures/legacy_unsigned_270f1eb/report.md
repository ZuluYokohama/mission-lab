# Mission execution evidence

Mission: `legacy_fixture`
Plan: `6aee297831fcb5866440bb0b4c7ed0b5eba137efd99fd4fe74256285fb7696ee`

Result: accepted within the registered operator contract

Machine verification is not human review or independent scientific reproduction.

Requirement prose is retained for alignment; its semantic adequacy remains unreviewed.

## Resource and measurement boundary

Worker cap: 1. Aggregate read-buffer reservation: 65536 bytes.
Cost-estimate origin: `injected_fixture`.
Reservations constrain registered worker count and explicit input buffers. Total process memory, physical core use, and energy are unknown.
Worker processes receive a deny-by-default environment: platform start names when set, two controller-forced interpreter flags, and per-task resource grants. Observations list those names and do not retain values.
Predicted schedules use a bounded heuristic. Observed durations include worker startup after launch; costs may differ from predictions.

## Outcomes

| Status | Count |
|---|---:|
| succeeded | 4 |
| failed | 0 |
| blocked | 0 |
| timed_out | 0 |

| Task | Status | SHA-256 / verification | Bytes |
|---|---|---|---:|
| hash:alpha | succeeded | ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad | 3 |
| manifest | succeeded | — | — |
| reference:alpha | succeeded | ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad | 3 |
| verify:alpha | succeeded | ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad | 3 |

## Observations

| Task | Estimated duration (ns) | Observed duration (ns) | Reserved buffer bytes | Environment names |
|---|---:|---:|---:|---|
| reference:alpha | 1009 | 31917271 | 3 | HOME, LANG, LC_ALL, PATH, PYTHONDONTWRITEBYTECODE, PYTHONUNBUFFERED |
| hash:alpha | 1003 | 31697275 | 3 | HOME, LANG, LC_ALL, PATH, PYTHONDONTWRITEBYTECODE, PYTHONUNBUFFERED |
| verify:alpha | 1000 | 31962866 | 0 | HOME, LANG, LC_ALL, PATH, PYTHONDONTWRITEBYTECODE, PYTHONUNBUFFERED |
| manifest | 1000 | 31696664 | 0 | HOME, LANG, LC_ALL, PATH, PYTHONDONTWRITEBYTECODE, PYTHONUNBUFFERED |

## Limits and alternative explanations

Different cache state, process startup, storage contention, and estimate error can explain timing differences.
Matching digests supports the file-audit contract. It establishes no numerical-model, energy-saving, or general software-generation claim.
An llm:grok receipt records the request, response hashes, provider-reported token counts, latency, and HTTP status. It does not score model quality or establish that the response is true.
A gate predicate compares declared receipt fields. Passing a gate is not a quality score, a signature, or a human review of the mission.
Offline replay regenerates recorded outcomes and re-evaluates gate predicates from archived receipts. It performs no new independent execution and does not call a model.
