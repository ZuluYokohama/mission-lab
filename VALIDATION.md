# Release validation — 0.1.0 preview

These are results from the clean public derivative, not copied claims from the original study. Raw local evidence is excluded from Git because it contains process and filesystem metadata. `validation_summary.json` is a generated projection, not the complete evidence archive. Run `demo` in a fresh output directory to create your own complete receipts.

**Package tests: 143 passed, 2 skipped, 13 subtests passed** using Python 3.12.14 and the existing research test environment. Two symbolic-link cases were skipped for Windows privilege limits; separate reparse checks passed. Final changes after these tests were documentation and a license notice, with runtime/test code unchanged.

Both fresh demonstration runs completed all ten tasks. Digest and byte count agreed exactly across the same three fixtures. The pattern binding changed from `whole_v1` to `stream_v1`. Replaying each run twice produced identical regenerated results and tables.

| Run | Worker limit | Buffer limit (bytes) | Controller-observed peak reserved bytes | Succeeded tasks |
|---|---:|---:|---:|---:|
| baseline | 2 | 4194304 | 139265 | 10 |
| revised | 2 | 65536 | 40960 | 10 |

| Fixture | Bytes | SHA-256 |
|---|---:|---|
| abc | 3 | `ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad` |
| empty | 0 | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| pattern | 131073 | `909d26d209cbd700c2e7266ec398959cd4fff4aab86f531e4912f58ca1a02c2a` |

Buffer reservations and worker observations come from the controller itself. Total process memory, physical cores and energy remain unknown. Cost estimates and the budget change are injected; no measured performance advantage or global optimality is established. The primary and reference paths share `hashlib`.

Machine tests, agent review and replay are not human review or independent reproduction. Full V&V remains pending; see KNOWN_ISSUES.md and VV_PLAN.md.

Final package source identity: `706806ec40d37608dd146e1a5f3f115f69dab7f185dfef39cbb7a3f18489d979`.

The original controller source was byte-for-byte preserved. The v0.2 delivery, v0.3–v0.5 locks, research delivery, v0.6 integrity and Language Coherence lock were checked locally and passed. Those studies and their evidence are not part of this repository.
