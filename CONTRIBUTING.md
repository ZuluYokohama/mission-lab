# Contributing

Mission Lab is an experimental controller for bounded file-audit missions. Read the [contract](research/mission_lab_v0_1/CONTRACT.md), [known issues](KNOWN_ISSUES.md) and [development guide](docs/DEVELOPMENT.md) before changing behavior.

Start with a small issue or pull request that states the requirement, expected behavior and evidence needed to accept the change. Surface material ambiguity before implementing it. Keep unrelated changes separate and preserve failure evidence. Model execution, hardware backends and general software generation require a separately reviewed specification.

For a pull request:

1. Explain the concrete problem and resulting behavior. Link the relevant requirement or issue.
2. Add meaningful regression coverage for changed behavior. Report the commands, environment, results and skips you actually observed.
3. Run the repository checks and package tests in the [development guide](docs/DEVELOPMENT.md). Run a fresh demonstration when execution, planning, evidence or fixture behavior changes.
4. Review the diff for private paths, credentials, archive inventories, models and generated evidence. Keep these out of the contribution.
5. Address review findings or explain why they do not apply. The maintainer makes the final acceptance decision.

CodeRabbit feedback, when the integration is enabled, is advisory. Passing checks or bot approval does not establish human review, independent reproduction or complete V&V. The review and release workflow is described in [REVIEW_AND_RELEASE.md](docs/REVIEW_AND_RELEASE.md).

## Contribution licensing

By submitting a contribution for inclusion, explicitly confirm in the pull request that you have the right to contribute it and offer your contribution under [PolyForm Noncommercial 1.0.0](LICENSE.md), with the repository's [required notice](NOTICE.md). This grants the applicable license; it does not transfer copyright. Disclose third-party material and its terms before inclusion. Do not assume that publicly accessible code, papers or datasets can be relicensed.

For a suspected vulnerability, follow [SECURITY.md](SECURITY.md) before sharing details publicly.
