# LLM variables, operators, artifacts and mission boundaries

This is a technical extension map. The implemented v0.1 operators only hash, compare and assemble tiny fixture artifacts. No LLM runtime, tensor library, retrieval engine or trainer is activated by this document.

## Executable representation

A complete system specification separates data snapshots and transforms, tokenizer/template, architecture graph, learned parameters, training state, request state, external information, runtime/backend, hardware environment and evaluation contract. These components form an artifact graph with explicit parent identities. A checkpoint alone does not preserve the whole system.

An inference interface can be written as `(logits, next_state) = forward(parameters, graph, token_ids, request_state, runtime)`. A separate sampler selects tokens. A separate application executes authorized tool actions and returns observations. The operator graph is logical; a lowered execution plan can fuse operations, choose layouts, schedule kernels and place tensors on devices without materializing every textbook intermediate.

## Representative decoder transformer

Use row-vector multiplication `Y = XW`. Let batch be B, new tokens T, cached tokens S, vocabulary V, hidden width d, layers L, query heads Hq, KV heads Hkv, head width dh and FFN width f. In this example `Hq * dh = d`. Framework weight storage can be transposed; semantic axes must accompany shapes.

| Stage | Variables and operator | Artifact/state boundary |
|---|---|---|
| Data | Clean, deduplicate, mix, split and tokenize source records | Dataset revisions, license/scope, transforms, split hashes, sampling recipe |
| Input | Serialize messages through template; encode to IDs `[B,T]` | Tokenizer algorithm/vocabulary, special IDs, template and normalization |
| Embedding | Lookup `E[ids]`, with `E:[V,d]`, producing `[B,T,d]` | Learned token embeddings; distinct from retrieval vectors |
| Normalization | RMSNorm uses learned scale and root-mean-square plus epsilon | Scale is learned; epsilon and norm type are configuration |
| Projections | `Q=UWq`, `K=UWk`, `Values=UWv` | Weight identity and axes; query/KV head counts |
| Position | RoPE rotates query/key pairs using token positions | Frequency/layout/scaling configuration; cache position identity |
| Attention | `softmax(QKᵀ/sqrt(dh)+mask) @ Values` | Mask convention, causal offsets, padding, packed-document boundaries |
| Cache | Append current keys/values; preserve compatible prefix state | Per-request activation state; never assume all architectures share this cache |
| FFN | SwiGLU: `(SiLU(ZWg) ⊙ ZWu)Wd` | Hidden width, activation, gating and parameter identities |
| Residual | Add attention and FFN outputs to residual stream | Ordering, normalization placement, architecture revision |
| Output | Final norm and vocabulary projection; logits `[B,T,V]` | Tied versus separate output weights |
| Sampling | Temperature, truncation, constraints, RNG and stopping | Generation policy and request state, not training updates |

A common block is `U=RMSNorm(X); Y=X+Attention(U); Xnext=Y+SwiGLU(RMSNorm(Y))`. Many other arrangements exist. The [original transformer paper](https://arxiv.org/abs/1706.03762) describes attention architecture; modern decoder choices require their own configuration records.

For GQA, several query heads share each KV head. MHA has `Hkv=Hq`; MQA has one KV head. A conventional cache layer has shape `[B,Hkv,S,dh]` separately for keys and values. Cached chunks need absolute position offsets in their causal mask. RoPE keys already in cache must not be rotated a second time. Consult the [GQA paper](https://aclanthology.org/2023.emnlp-main.298/) and [cache documentation](https://huggingface.co/docs/transformers/main/en/cache_explanation) for their scoped mechanisms.

Prefill computes the prompt and fills caches. Decode usually processes one new token and extends the cache. A token probability is a vocabulary distribution, not automatically the probability that a proposition is true. Matching one answer or argmax is weaker than matching the full distribution or future continuations.

## Learning operators

Teacher-forced training shifts token targets and minimizes cross-entropy. A loss mask selects which predictions contribute to the objective; an attention mask selects visible information. Autodiff composes local vector-Jacobian products. For `Y=XW`, backward uses `dX=dY Wᵀ` and `dW=Xᵀ dY`. Embedding updates accumulate at selected token IDs; tied parameters accumulate all uses.

An optimizer such as AdamW maintains moment state, uses a learning-rate schedule and applies decoupled weight decay. Training resume requires relevant weights, optimizer/scheduler state, RNG, step and data cursor, not only inference weights. Accumulation, clipping, precision and distributed collectives belong in the recipe. [AdamW documentation](https://docs.pytorch.org/docs/stable/generated/torch.optim.AdamW.html).

SFT learns from demonstrations. Preference and reinforcement objectives introduce preferred/rejected examples, reference policies, rewards, verifiers or policy constraints. LoRA adds a low-rank update `W=W0+sAB`; its adapter normally requires the compatible base. Distillation and pruning create altered models whose behavior must be tested. [DPO paper](https://arxiv.org/abs/2305.18290), [LoRA paper](https://arxiv.org/abs/2106.09685).

MoE adds routing and selected expert executions, separating total from active parameters. Recurrent/state-space models use different retained state. Latent attention, sliding windows, multimodal encoders and diffusion generation need architecture-specific state and operators. The transformer example and conventional KV formula are not universal.

## Retrieval and tool surfaces

Retrieval embeddings are produced by a specified encoder, pooling and normalization procedure. Search/rerank supplies document chunks to the prompt while generator weights can remain unchanged. Record corpus/chunk revisions, encoder revision, similarity metric and index construction. Vector proximity is not a truth predicate. [RAG paper](https://arxiv.org/abs/2005.11401).

For tools, generation proposes a structured call; the application's parser, permission boundary and executor perform the action. Results enter later context. Inputs from documents, repositories and tool results are evidence/data, not authority to change the mission or permissions. Model state, controller knowledge, external state and actual side effects must have separate ownership.

## llama.cpp, GGML and GGUF

llama.cpp provides model loading, architecture-specific graphs, request processing and sampling/serving. GGML provides tensor operators, allocation, scheduling and backend execution. GGUF is a metadata/tensor container, supporting floating or quantized encodings. Compatibility requires the corresponding graph and backend implementations. [llama.cpp](https://github.com/ggml-org/llama.cpp), [GGUF specification](https://github.com/ggml-org/ggml/blob/master/docs/gguf.md).

The release pipeline is checkpoint tensors plus configuration/tokenizer → architecture-aware conversion → GGUF → optional quantization → loading/device placement → graph/kernel execution → prefill/decode/cache → sampling/application output. Conversion can rename, reshape, permute and re-encode tensors. Test semantics and numerical tolerances across that boundary.

Quantization stores approximate values through codes, scales and format-specific metadata. Nominal bit width is not the complete file size or runtime memory. Dequantization cannot recover discarded information. Kernel fusion can reduce traffic without changing the intended mathematical operation; finite-precision effects still require verification. An optimized implementation's speed is conditional on model, workload, backend and hardware.

## Artifact consolidation

| Artifact | Preserved identity |
|---|---|
| Config | Architecture construction and declared dimensions |
| Tokenizer/template | Codec and message serialization |
| Named weight shards | Parameter names, shapes, encodings and sharding |
| Training checkpoint | Recorded optimization/resume state |
| Adapter | Update, target modules/rank and exact compatible base |
| Converted/quantized bundle | Transformation parents and runtime compatibility |
| Retrieval index | Corpus/chunk/encoder/metric lineage |
| Run record | Inputs, system, environment, scorer and actual observations |

Reconstructing shards, merging adapters, converting formats and packaging inference components are different transformations. None reconstructs omitted training data or historical optimization state. Every transformation gets parent hashes, implementation revision and output identity. Preserve licenses and scope rather than inferring rights from file extensions.

## Measurable boundaries and geometry

Keep task quality, latency, memory, operations, joules and dollars as separate dimensions. Weight bytes include encoding overhead. For conventional full-context KV, bytes are `2 * L * B * S * Hkv * dh * bytes_per_element`. Runtime memory also includes activations, workspaces and buffers. Changing Hkv is an architectural change and requires compatible weights.

Task cost includes preprocessing/retrieval, prefill, decode, validation and retries. End-to-end latency differs from a kernel microbenchmark. Per-request reduction does not establish aggregate preservation without request volume and shared lifecycle costs. No physical-resource measurement is supplied by this hash demo.

A proposed compression projection π preserves a declared observable q exactly only under a scoped contract such as `π(f(s,u)) = reduced_f(π(s),u)` and `q(s)=reduced_q(π(s))` over allowed states/inputs. Approximate preservation requires fixed error bounds. Define the observable and reachable scope before testing; preserving a fitted activation geometry alone need not preserve generation or future state.

Later geometry research needs operator/version audits, matched-corpus controls, causality checks, held-out transport and explicit sensitivity to basis, dimension and metric choice. Verify lineage before replaying code or generalizing signatures across domains. No private research archive is distributed.

Domain extensions should create explicit task contracts for language, code, mathematics, retrieval or multimodal work. Report a profile of demonstrated value and unresolved evidence. v0.1 supplies the controller/evidence scaffolding; it supplies no overall coherence score or universal mechanism conclusion.
