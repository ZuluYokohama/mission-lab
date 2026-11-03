# Runtime and evidence contract

The authoritative validators live in `core.py`, `planner.py` and the executor. This document explains their intent; it does not relax runtime validation.

## Mission

`schema_version` is 1. `mission_id` is an identifier. Each requirement has a unique `id`, nonempty `text`, a nonempty list of `acceptance_criteria`, and an `evidence_refs` list. Each unresolved item contains `id`, `question`, and `affects`, where `affects` is intent, acceptance, authority or tradeoff. Each of one to eight file inputs records `id`, `path`, `size_bytes` and lowercase 64-character SHA-256.

Runtime records reject duplicate JSON keys, unknown fields, malformed identifiers, non-finite numbers and Boolean values passed as integer bounds. Inputs are identity checked against actual file size and bytes for execution planning. Archive metadata inventory is a separate operation and performs no content hash.

An explicit graph, when supplied, must agree with registered semantics. Each node declares `id`, `operation`, `depends_on`, `outputs`, `state_reads`, `state_writes`, and an `input_id` when required. An `llm:grok` node may omit `input_id` when it carries inline prompt text. Each node owns one output; state reads require producer ancestry. Every input requires exactly one primary hash, reference hash and verification. One manifest covers all verifications. `llm:grok` is an additional sink and does not replace that path. A graph compiled because the mission omitted `tasks` stays hash-only.

## Registered operations

| Operation | Candidate role | State contract |
|---|---|---|
| `sha256` | Whole-file or bounded streaming implementation | Reads the declared immutable file; emits digest and byte count |
| `reference_sha256` | Separate fixed 8 KiB read path | Reads the same declared file; emits comparable digest and byte count |
| `verify_digest` | Compare candidate, reference and declared identity | Reads both producer outputs; emits a passed verification only on agreement |
| `assemble_manifest` | Aggregate verified artifacts | Reads every verification; emits the final artifact manifest |
| `llm:grok` | One chat-completions call, or one local fixture read | Reads a prompt and optional fixture; emits a receipt and hash-addressed response artifacts |

The reference is a separate registered execution path using standard-library hashing, not an independent SHA-256 library or a human reproduction. Candidate and operator revisions are explicit; unsupported revisions fail validation. `llm:grok` has one candidate, `chat_v1` revision `1`. Its schedule estimate is `fixed_task_ns`. Observed latency is a separate receipt field and is not a measured model duration.

## Registered chat call

`llm:grok` declares `model`, `max_output_tokens`, `temperature`, `timeout_seconds`, `max_response_bytes`, and `transport`, plus exactly one of inline `prompt` or a prompt-file `input_id`. Unknown fields are rejected. `model` matches `grok-` followed by 1 to 64 characters from `[A-Za-z0-9._-]`, and the first of those characters is alphanumeric. That pattern is a naming bound. It does not state that the named model is available or suitable. `max_output_tokens` is an integer from 1 to 4096 and is sent as `max_tokens`; it is not a local measurement of output bytes. `temperature` is a multiple of 0.01 in [0, 2]. A Boolean is rejected, and a whole number is stored as an integer. `timeout_seconds` is finite and in [0.1, 120], and must be at least one second below the profile timeout so a socket timeout can be recorded before the executor kills the worker. `max_response_bytes` is an integer from 1 to 65536.

Inline prompt text is 1 to 4096 UTF-8 bytes and contains no NUL. A prompt file is a mission input of that same size. Transport is `xai` or `fixture`. The mission cannot name a URL. Transport `fixture` requires `fixture_id`, a different mission input of 1 to `max_response_bytes` bytes containing one chat-completions JSON object. The node's dependencies are exactly the `verify_digest` nodes of the prompt file and, when present, the fixture file. An inline prompt with transport `xai` has an empty dependency list. `state_reads` must match those outputs.

Transport `xai` posts compact sorted JSON to `https://api.x.ai/v1/chat/completions` with `urllib`. The body is `model`, one user message, `max_tokens`, `temperature`, and `stream` false. The bearer token is the worker environment variable `XAI_API_KEY`, exact spelling. That name must be granted to the task. A fixture task, and any task that is not this operator on transport `xai`, cannot receive it. An unset grant fails before process start. The worker does not write the key into the receipt or the stored response. Transport `fixture` reads the declared file, records HTTP status 200, and does not open a socket or read `XAI_API_KEY`.

`SSL_CERT_FILE`, `SSL_CERT_DIR`, `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, and `NO_PROXY` are not platform start names. A task that needs one must grant it. urllib will use a granted proxy, and that proxy can see the bearer token. Redirects are refused, so `Authorization` is not followed to another host. A timeout, a non-2xx status, malformed JSON, or a body over the declared cap fails the task. The read keeps at most the cap; one extra byte detects overflow and is discarded.

A successful receipt records the model id, transport, request parameters, prompt hash and byte count, request-body hash, response-body hash and size, HTTP status, latency in nanoseconds, provider-reported `prompt_tokens`, `completion_tokens`, and `total_tokens`, the response model string, finish reason, and the hash and size of the assistant text. It does not record prompt text, the key, or the assistant text. The response body is stored at `llm_responses/<sha256>` and the assistant text at `llm_content/<sha256>`. Success requires HTTP 200, one choice, an assistant string, a printable-ASCII finish reason of 1 to 64 characters, a printable-ASCII response model of 1 to 128 characters, and non-negative integer usage fields of at most 1e9. Extra usage fields are ignored and not recorded.

The read-buffer reservation is the larger of the prompt-file size and the fixture-file size, or zero when the prompt is inline and transport is `xai`. The response cap is not added to that reservation. Prompt and fixture inputs are copied into the run input snapshot so replay does not reread the live files. Replay rebuilds the request bytes, checks the recorded hashes against the archived response and content, and, for a fixture, checks that those response bytes equal the archived fixture. It does not call the network.

A unit test may pass `urllib_chat` a loopback `http://127.0.0.1` URL. That argument is not a mission field. The optional live smoke test runs only when `MISSION_LAB_GROK_LIVE=1` and `XAI_API_KEY` is set. CI does not set that flag and does not need a key. The receipt does not score quality, truth, or fitness of the response.

## Resource profile and estimates

Fields are `schema_version`, `workers`, `read_buffer_bytes`, `timeout_seconds`, `estimate_origin`, and optional `cost_model` and `worker_env`. Worker slots are 1..8. Read-buffer reservation bytes are 1..2^40. Timeout seconds must be finite, positive and at most 3,600. Cost origins are `injected_fixture` or `measured`; measured profiles require explicit supplied costs.

The cost model declares whole, stream and reference nanoseconds per byte plus fixed task nanoseconds. Defaults are injected illustrative costs. Supplying a measured label does not independently authenticate the measurement; provenance must substantiate any scientific interpretation. Estimated schedule duration and actual wall-clock observations remain separate fields.

The executor bounds its worker count and sum of registered read-buffer reservations. Whole-file reservations depend on input size; streaming and reference paths have registered chunk bounds. An `llm:grok` reservation is only the declared prompt or fixture bytes. These bounds exclude interpreter and library overhead, incidental metadata/state allocations, process RSS, OS caches, cores, GPU allocations, response bytes beyond the separate cap, and energy.

## Worker environment

Registered workers do not inherit the controller process environment. The default is deny. For each launch the executor builds a new mapping with three parts:

- platform start names that are already set on the controller, limited to names used to start CPython and locate imports
- `PYTHONDONTWRITEBYTECODE=1` and `PYTHONUNBUFFERED=1`, set by the controller rather than copied
- names the resource profile grants to that task, copied from the controller when they are set to strings

On POSIX the start names are `HOME`, `LANG`, `LC_ALL`, `LC_CTYPE`, `PATH`, `PYTHONCOERCECLOCALE`, `PYTHONHOME`, `PYTHONIOENCODING`, `PYTHONNOUSERSITE`, `PYTHONPATH`, `PYTHONSAFEPATH`, `PYTHONUTF8`, `TMPDIR` and `TZ`. On Windows they are `APPDATA`, `HOMEDRIVE`, `HOMEPATH`, `LOCALAPPDATA`, `PATH`, `PATHEXT`, `PYTHONCOERCECLOCALE`, `PYTHONHOME`, `PYTHONIOENCODING`, `PYTHONLEGACYWINDOWSFSENCODING`, `PYTHONLEGACYWINDOWSSTDIO`, `PYTHONNOUSERSITE`, `PYTHONPATH`, `PYTHONSAFEPATH`, `PYTHONUTF8`, `SYSTEMDRIVE`, `SYSTEMROOT`, `TEMP`, `TMP`, `USERPROFILE` and `WINDIR`. A name is omitted when it is unset. The authoritative lists are `platform_env_names()` and `FORCED_WORKER_ENV` in `core.py`. Windows matches allowlist, grant, and reserved names without regard to case, and the executor passes and records those names in uppercase, the spelling CPython reports for the child. A case variant cannot grant a reserved name or copy a variable that was not granted.

Optional `worker_env` contains `declared` and `grants` only. `declared` is a list of at most 32 environment names. `grants` maps at most 64 task ids to nonempty lists of at most 32 of those names. Validation rejects undeclared names, duplicate names, malformed names, embedded values, names that collide with the platform or forced sets, unknown fields, and grants whose task id is not in the executable graph. A declared name that no task is granted is not copied. An unset or non-string granted name fails that task before a process is created; an empty string is not substituted.

Plans and resource profiles record grant names, not values. Plan normalization does not depend on the host operating system. Each launch event and task observation records sorted `environment_names` and no values. Replay rejects a receipt that names a variable outside the two platform lists, the forced flags, and that task's grants, or that omits a granted name or a forced flag. It also rejects an observation whose names differ from the launch event, and a receipt that repeats one name under another case. Name comparison folds case, so a Windows record can be replayed on POSIX. That check does not prove which platform names were set or the exact spelling a child reported, and it does not observe the child process.

This is process-environment filtering for the registered worker command. It is not a system-call sandbox, a credential store, or a statement that a worker which received a grant cannot disclose that value. TLS trust stores, proxy names, service endpoints, and `XAI_API_KEY` are outside the start allowlist. `llm:grok` with transport `xai` must be granted `XAI_API_KEY`. A granted proxy can see that token.

## Identity, output authority and replay

Plans record source hashes and a canonical content identity. Inputs and source code must retain their identities before execution. Plans, revisions and evidence outputs use fresh directories confined beneath package `evidence/`. Parent records are retained; earlier evidence is never overwritten. Symlink/junction traversal and unsafe output locations are refused.

Retained run evidence binds outputs, observed status and elapsed times to a plan. Replay validates identities and reconstructs predicates from those retained records. A successful replay is evidence-integrity validation, not a new execution measurement. Missing, malformed or altered observations must fail rather than be synthesized.

## Archive inspection

The public-release inventory root is the package fixture directory. Mission input paths are confined to the checkout and package fixtures. Descendants may be inspected. A keyword-only `allowed_roots` seam exists for Python unit tests and is absent from the CLI. Files cannot be used as inventory roots. All encountered reparse points are refused conservatively, even if their targets would remain in the archive.

`max_entries` counts files and directories and is 1..100,000. `max_depth` is 1..64. At a limit, `complete` is false and reasons are retained. Records contain relative path, kind, observed bytes for files, UTC modification time, extension and depth. The snapshot is non-atomic and cannot prove source integrity, lineage, executability, scientific validity or total counts when truncated. It reads no archive contents.

## Human and external boundaries

The code cannot resolve unclear intent, confer account permissions, establish human review, or issue arbitrary shell commands. It does not load local weights, train a model, mutate an external connector, publish, or edit its own source. `llm:grok` records one remote chat call or one local fixture read under the bounds above. Existing frozen research packages are not imported or changed.
