# Review and release

The maintainer is `ZuluYokohama`. Repository configuration assigns code ownership to that account. A paid GitHub or CodeRabbit subscription does not establish that an integration, repository permission or branch rule is enabled. Check the actual published repository settings before relying on them.

## Review a change

Use a focused branch and pull request with the problem, intended behavior, affected requirements, observed validation and remaining uncertainty. Inspect the full diff, including fixtures and configuration. New evidence must refer to the source and assumptions actually tested.

The configured check names are:

- `Repository checks`
- `Tests (ubuntu-latest)`
- `Tests (windows-latest)`

These checks verify repository rules and execute the test and fixture workflow. They do not complete the independent [V&V plan](../VV_PLAN.md). Require applicable checks before merging once the remote workflow is enabled and its names are confirmed.

[CodeRabbit configuration](../.coderabbit.yaml) supplies project-specific review guidance. When installed and authorized for the repository, use its findings as additional review input. Keep the bot advisory: it does not decide requirement interpretation, approve scientific claims or replace final maintainer acceptance. No automatic merge is part of this workflow.

For a sole maintainer, do not configure a mandatory second-human approval that cannot be satisfied. When another human reviewer is available, document their role and actual review. Claims of inter-reviewer reliability require independent human reviews and a specified comparison method.

## Prepare a release

1. Identify the exact commit, intended scope and applicable license notices. Check that the release does not include private archives, environments, models or raw local evidence.
2. Run the repository checks and applicable tests from [DEVELOPMENT.md](DEVELOPMENT.md). Confirm hosted results on each supported CI platform after publication. Record failures and skips.
3. If package source changed, generate fresh plans and demonstration evidence. Update the public validation projection and provenance with the new identity and actual results. Preserve prior evidence unchanged.
4. Review [KNOWN_ISSUES.md](../KNOWN_ISSUES.md), [VALIDATION.md](../VALIDATION.md) and [CHANGELOG.md](../CHANGELOG.md). Separate completed work from proposed work, measured timings from injected estimates, and machine checks from human acceptance.
5. Inspect the staged files and license scope. The maintainer decides whether the scoped preview is ready, then publishes the intended commit and verifies the remote commit identifier.

Tag or announce a release only after its publication is confirmed. The current documentation does not assert that a remote repository, branch protection or CodeRabbit installation has been verified. Never describe a passing CI run as independent reproduction or safety certification.
