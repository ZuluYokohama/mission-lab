# GitHub and CodeRabbit setup

This file is the intended repository setup, not evidence that remote settings are active. The intended public repository is `ZuluYokohama/mission-lab`. No paid feature, integration access or completed check is inferred from subscription status.

## Main branch and pull requests

Use `main` for reviewed changes and short-lived topic branches for work. Use squash merges, delete merged topic branches, and keep automatic merging disabled. Require a pull request, resolved review conversations, and passing checks for `main`; block force pushes and branch deletion. Do not require another human approval while there is only one human maintainer. CODEOWNERS routes review to that maintainer and does not prove independent review.

After these checks have actually completed on GitHub, require their exact names and trusted application source:

- `Repository checks`
- `Tests (ubuntu-latest)`
- `Tests (windows-latest)`

The recommended ruleset applies to `refs/heads/main`. Required checks should be up to date with the base branch. Protect administrator changes too, with any emergency bypass recorded in the PR. Avoid adding a nonexistent check or mandatory reviewer that would prevent all merges.

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
