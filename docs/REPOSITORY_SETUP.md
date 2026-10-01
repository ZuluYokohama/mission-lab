# GitHub and CodeRabbit setup

This file describes the repository policy and a dated observation of its setup. The public repository is [ZuluYokohama/mission-lab](https://github.com/ZuluYokohama/mission-lab). No paid feature, integration access or completed check is inferred from subscription status.

## Verified setup on October 1, 2026

The GitHub settings UI showed the following after configuration:

- `main` is the default branch. Squash merging is the only enabled merge method; automatic merging is off. Merged topic branches are deleted automatically and can be restored.
- The active [main-quality-gate ruleset](https://github.com/ZuluYokohama/mission-lab/settings/rules/24327753) targets the default branch, currently `main`, with no bypass actors. It requires a pull request, resolved conversations, linear history, and the three checks listed below from GitHub Actions, with the branch up to date. Force pushes and branch deletion are blocked.
- Required approving reviews are zero for the initial solo-maintainer workflow. This does not constitute human acceptance or independent review.
- Private vulnerability reporting, the dependency graph and Dependabot alerts are enabled. Secret Protection and push protection were already enabled and remain enabled.

[CI run 36911908122](https://github.com/ZuluYokohama/mission-lab/actions/runs/36911908122) completed successfully for the initial organization commit `2cc0ff3840fce455a8df3d7dcdb5208d0a71ab27`, including the baseline/revision demonstration and replay on both operating systems. Later commits require their own checks.

CodeRabbit's [first review](https://github.com/ZuluYokohama/mission-lab/pull/1#pullrequestreview-5384274268) reported the repository configuration, assertive profile and Essentials plan. Its `CodeRabbit` status was successful while two actionable findings remained, so it is advisory and is not a required passing check. Review completion does not mean findings are resolved. These observations are a dated setup record, not continuous monitoring of account or repository settings.

## Main branch and pull requests

Use `main` for reviewed changes and short-lived topic branches for work. Use squash merges, delete merged topic branches, and keep automatic merging disabled. Require a pull request, resolved review conversations, and passing checks for `main`; block force pushes and branch deletion. Do not require another human approval while there is only one human maintainer. CODEOWNERS routes review to that maintainer and does not prove independent review.

After these checks have actually completed on GitHub, require their exact names and trusted application source:

- `Repository checks`
- `Tests (ubuntu-latest)`
- `Tests (windows-latest)`

The ruleset follows the default branch, currently `refs/heads/main`. Required checks should be up to date with the base branch. Protect administrator changes too, with any emergency bypass recorded in the PR. Avoid adding a nonexistent check or mandatory reviewer that would prevent all merges.

## CodeRabbit

Confirm the existing paid CodeRabbit account's GitHub App installation includes this repository. Selected-repository installations may need the new repository added. Read the displayed permissions before changing app access. The committed configuration enables reviews on ready PRs and incremental updates, excludes raw generated evidence and binary fixtures, and disables automatic approval/change-request workflow and the listed code-generation finishing actions.

Observe a real review on the final PR commit and verify that the repository configuration was applied. CodeRabbit review progress is separate from findings and separate from maintainer acceptance. Only add the actual observed CodeRabbit status as a required check after confirming its behavior and expected application; do not guess its name.

## Actions and public information

Use GitHub-hosted runners for public PRs. CI requests only read access to repository contents, uses immutable action revisions, sets finite timeouts and cancels superseded runs. It uses `pull_request`, not privileged PR execution. No repository secrets, model downloads, inference, external publication or automatic raw-evidence uploads are needed by this workflow. Development dependency installation happens in disposable hosted runners; runtime remains standard-library-only.

Dependabot is configured for monthly reviewed updates to Actions and pinned test packages, with three open PRs per ecosystem. Do not enable automatic merges for those updates. Enable private vulnerability reporting before advertising it as an available reporting route. Keep repository descriptions and topics explicit about the experimental, source-available, noncommercial scope.

## Official references

- [GitHub repository rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/creating-rulesets-for-a-repository)
- [GitHub secure use of Actions](https://docs.github.com/en/actions/reference/security/secure-use)
- [CodeRabbit GitHub integration](https://docs.coderabbit.ai/platforms/github-com)
- [CodeRabbit configuration](https://docs.coderabbit.ai/reference/configuration)
- [CodeRabbit automatic review behavior](https://docs.coderabbit.ai/configuration/auto-review)
