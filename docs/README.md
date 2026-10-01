# Documentation map

Start with the repository [README](../README.md) for scope, licensing and runnable examples.

| Need | Document |
|---|---|
| Understand the accepted operator and resource boundaries | [Contract](../research/mission_lab_v0_1/CONTRACT.md) |
| Understand the study procedure and evidence records | [Protocol](../research/mission_lab_v0_1/PROTOCOL.md) |
| See the implementation graph | [Architecture](../research/mission_lab_v0_1/ARCHITECTURE.mmd) |
| Change and test the code | [Development](DEVELOPMENT.md) and [contribution guide](../CONTRIBUTING.md) |
| Review and prepare a release | [Review and release](REVIEW_AND_RELEASE.md) |
| Configure hosting and paid review integration | [Repository setup](REPOSITORY_SETUP.md) |
| Check observed results and unresolved gaps | [Validation](../VALIDATION.md) and [known issues](../KNOWN_ISSUES.md) |
| Understand proposed next work | [Roadmap](ROADMAP.md) and [V&V plan](../VV_PLAN.md) |
| Trace the public derivative and design references | [Release provenance](../RELEASE_PROVENANCE.json), [source registry](../research/mission_lab_v0_1/SOURCES.json) and [third-party notices](../THIRD_PARTY_NOTICES.md) |
| Place the controller in a broader LLM system | [LLM systems map](../research/mission_lab_v0_1/LLM_SYSTEMS_MAP.md) |
| Report a security concern | [Security](../SECURITY.md) |

## Repository layout

`research/mission_lab_v0_1/` contains the controller, package tests, small deterministic fixtures and the documents included in its source identity. `docs/` contains repository workflow guidance. `.github/` contains collaboration templates and hosted-check configuration. Root `tools/` contains repository maintenance checks.

Generated runs belong under `research/mission_lab_v0_1/evidence/`, which is ignored by Git. They can contain private filesystem and process metadata. The root validation summary is a public projection, not the complete raw evidence archive.
