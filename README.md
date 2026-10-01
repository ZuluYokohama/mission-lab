# Mission Lab v0.1 — experimental public preview

Mission Lab connects a structured file-audit specification to an operator DAG, a bounded resource schedule, worker execution, verification receipts and evidence-driven revision. It runs registered SHA-256 and manifest operations. Runtime uses only the Python standard library.

This preview has package tests and a reproducible fixture demonstration. Independent V&V and human intended-use validation are pending. Read [KNOWN_ISSUES.md](KNOWN_ISSUES.md) before relying on its validators. It is not a security sandbox or a safety-critical controller.

## License

Original project code, fixtures and project-authored documentation are available under [PolyForm Noncommercial 1.0.0](LICENSE.md). The full license defines permitted purposes and redistribution obligations, including permissions for listed educational, public research, charitable and government organizations regardless of funding. Uses outside that grant require separate permission from relevant rights holders. This is public source-available software, not OSI open source. See [NOTICE.md](NOTICE.md) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Run from the repository root

Python 3.12 is the tested version. No runtime installation or model download is needed.

```powershell
python -B -m research.mission_lab_v0_1 --help
python -B -m research.mission_lab_v0_1 validate --mission research/mission_lab_v0_1/fixtures/mission.json
python -B -m research.mission_lab_v0_1 demo --out evidence/my_demo_001
python -B -m research.mission_lab_v0_1 replay --run research/mission_lab_v0_1/evidence/my_demo_001/baseline_run
python -B -m research.mission_lab_v0_1 inspect --root research/mission_lab_v0_1/fixtures --out evidence/my_inventory_001
```

Writing commands require a fresh directory under `research/mission_lab_v0_1/evidence/`. Reusing an output directory is refused. Generated evidence is local and ignored by Git: it can contain filesystem paths, PIDs, executable locations and timestamps. Inspect it before sharing.

The other commands are `plan`, `run` and `replan`; use their `--help` for arguments. Missions accept 1–8 input files and a closed graph of at most 64 nodes. The public derivative confines file inputs to the checkout and confines metadata inspection to package fixtures. Broader archive adapters require a separately reviewed change.

## Demonstration and boundaries

An empty file, `abc`, and a deterministic 131,073-byte pattern are audited. The baseline allows two workers and a 4 MiB aggregate declared read-buffer reservation. A revision injects a 64 KiB budget. Whole-file and 32 KiB streaming candidates must agree with a separate 8 KiB read path on exact digest and byte count; both paths share `hashlib`. The tighter budget changes the pattern binding while preserving required results.

Scheduling costs and the budget change are injected fixtures. Actual timings are observations. No speedup, optimality, RSS, physical-core or energy-saving claim is established. Replay regenerates recorded conclusions; it is not another execution or independent reproduction. Machine validation is not human review.

## Tests and next validation

Tests require pytest, which is separate from the dependency-free runtime:

```powershell
python -B -m pytest research/mission_lab_v0_1/tests -q
```

See [VALIDATION.md](VALIDATION.md) for this release's actual results and [VV_PLAN.md](VV_PLAN.md) for the unimplemented independent validation campaign. See the package [contract](research/mission_lab_v0_1/CONTRACT.md), [protocol](research/mission_lab_v0_1/PROTOCOL.md), [architecture](research/mission_lab_v0_1/ARCHITECTURE.mmd), [LLM systems map](research/mission_lab_v0_1/LLM_SYSTEMS_MAP.md) and [source registry](research/mission_lab_v0_1/SOURCES.json).

## Release provenance

This is a clean derivative of a local exploratory controller. Only controller source, tiny deterministic fixtures, tests and documentation are included. Existing studies, original run evidence, private archive inventories, models, checkpoints and environments are excluded. Original local evidence was not rewritten. The release changes archive defaults, documentation and provenance notes; its plans have new source identities. No upstream implementation code was copied or executed.
