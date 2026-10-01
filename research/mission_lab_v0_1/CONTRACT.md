# Runtime and evidence contract

The authoritative validators live in `core.py`, `planner.py` and the executor. This document explains their intent; it does not relax runtime validation.

## Mission

`schema_version` is 1. `mission_id` is an identifier. Each requirement has a unique `id`, nonempty `text`, a nonempty list of `acceptance_criteria`, and an `evidence_refs` list. Each unresolved item contains `id`, `question`, and `affects`, where `affects` is intent, acceptance, authority or tradeoff. Each of one to eight file inputs records `id`, `path`, `size_bytes` and lowercase 64-character SHA-256.

Runtime records reject duplicate JSON keys, unknown fields, malformed identifiers, non-finite numbers and Boolean values passed as integer bounds. Inputs are identity checked against actual file size and bytes for execution planning. Archive metadata inventory is a separate operation and performs no content hash.

An explicit graph, when supplied, must agree with registered semantics. Each node declares `id`, `operation`, `depends_on`, `outputs`, `state_reads`, `state_writes`, and an `input_id` when required. Each node owns one output; state reads require producer ancestry. Every input requires exactly one primary hash, reference hash and verification. One manifest covers all verifications.

## Registered operations

| Operation | Candidate role | State contract |
|---|---|---|
| `sha256` | Whole-file or bounded streaming implementation | Reads the declared immutable file; emits digest and byte count |
| `reference_sha256` | Separate fixed 8 KiB read path | Reads the same declared file; emits comparable digest and byte count |
| `verify_digest` | Compare candidate, reference and declared identity | Reads both producer outputs; emits a passed verification only on agreement |
| `assemble_manifest` | Aggregate verified artifacts | Reads every verification; emits the final artifact manifest |

The reference is a separate registered execution path using standard-library hashing, not an independent SHA-256 library or a human reproduction. Candidate and operator revisions are explicit; unsupported revisions fail validation.

## Resource profile and estimates

Fields are `schema_version`, `workers`, `read_buffer_bytes`, `timeout_seconds`, `estimate_origin`, and optional `cost_model`. Worker slots are 1..8. Read-buffer reservation bytes are 1..2^40. Timeout seconds must be finite, positive and at most 3,600. Cost origins are `injected_fixture` or `measured`; measured profiles require explicit supplied costs.

The cost model declares whole, stream and reference nanoseconds per byte plus fixed task nanoseconds. Defaults are injected illustrative costs. Supplying a measured label does not independently authenticate the measurement; provenance must substantiate any scientific interpretation. Estimated schedule duration and actual wall-clock observations remain separate fields.

The executor bounds its worker count and sum of registered read-buffer reservations. Whole-file reservations depend on input size; streaming and reference paths have registered chunk bounds. These bounds exclude interpreter and library overhead, incidental metadata/state allocations, process RSS, OS caches, cores, GPU allocations and energy.

## Identity, output authority and replay

Plans record source hashes and a canonical content identity. Inputs and source code must retain their identities before execution. Plans, revisions and evidence outputs use fresh directories confined beneath package `evidence/`. Parent records are retained; earlier evidence is never overwritten. Symlink/junction traversal and unsafe output locations are refused.

Retained run evidence binds outputs, observed status and elapsed times to a plan. Replay validates identities and reconstructs predicates from those retained records. A successful replay is evidence-integrity validation, not a new execution measurement. Missing, malformed or altered observations must fail rather than be synthesized.

## Archive inspection

The public-release inventory root is the package fixture directory. Mission input paths are confined to the checkout and package fixtures. Descendants may be inspected. A keyword-only `allowed_roots` seam exists for Python unit tests and is absent from the CLI. Files cannot be used as inventory roots. All encountered reparse points are refused conservatively, even if their targets would remain in the archive.

`max_entries` counts files and directories and is 1..100,000. `max_depth` is 1..64. At a limit, `complete` is false and reasons are retained. Records contain relative path, kind, observed bytes for files, UTC modification time, extension and depth. The snapshot is non-atomic and cannot prove source integrity, lineage, executability, scientific validity or total counts when truncated. It reads no archive contents.

## Human and external boundaries

The code cannot resolve unclear intent, confer account permissions, establish human review, or issue arbitrary shell commands. It contains no model loading, training, inference, connector mutation, publication or autonomous code-editing operator. Existing frozen research packages are not imported or changed.
