# Development

Use Python 3.12 and run commands from the checkout root. The controller uses the standard library. Tests use `pytest==9.0.2`, pinned in [requirements-dev.txt](../requirements-dev.txt). An existing compatible environment can be used; otherwise create a virtual environment and install that test requirements file. Keep environments outside the package source directory.

## Checks

Run the same entry points used by CI:

```text
python -B tools/check_repository.py
python -B -m unittest discover -s tools/tests -v
python -B -m pytest research/mission_lab_v0_1/tests -q -p no:cacheprovider
python -B -m research.mission_lab_v0_1 validate --mission research/mission_lab_v0_1/fixtures/mission.json
```

Run tests sequentially. Some tests launch registered Python workers and create temporary evidence under the package. Symbolic-link tests may skip on Windows without the necessary privilege; report those skips. Hosted checks use Python 3.12 on both Windows and Ubuntu. A local Windows result does not establish an Ubuntu result.

For changes affecting planning, execution or evidence, run a fresh demonstration and replay both runs:

```text
python -B -m research.mission_lab_v0_1 demo --out evidence/dev_demo_001
python -B -m research.mission_lab_v0_1 replay --run research/mission_lab_v0_1/evidence/dev_demo_001/baseline_run
python -B -m research.mission_lab_v0_1 replay --run research/mission_lab_v0_1/evidence/dev_demo_001/revised_run
git diff --exit-code
```

Choose an unused output name for each demonstration. Existing evidence directories cannot be overwritten. Replay checks a recorded run; it is not a new independent execution. Review any intentional source diff separately if the final command reports changes.

## Source identities and layout

Keep the `research/mission_lab_v0_1` namespace and run from the checkout. Workspace confinement, source snapshots and worker imports depend on this layout. Installed wheels and editable packaging are not supported workflows for this preview.

The source manifest includes every package file except evidence and recognized caches, plus `research/__init__.py`. Documentation, fixture and test changes inside that boundary change the source identity. Create new plans and evidence after such changes. Retain earlier evidence and its original identity; do not update old receipts to make them match new code.

Repository workflow documents and configuration outside the package do not enter that package identity. The complete Git commit still identifies the repository release. `.gitattributes` keeps ordinary text in LF form and binary fixtures byte-preserved. `research/__init__.py` deliberately retains its existing CRLF bytes with `-text` to preserve the recorded package identity.

## Scope of development

The current operations audit file bytes. No model execution, accelerator benchmark or new runtime dependency belongs in routine validation. Changes to registered operations, effects, limits or acceptance criteria need an explicit contract change and evidence for that change. See the [roadmap](ROADMAP.md) for deferred work.
