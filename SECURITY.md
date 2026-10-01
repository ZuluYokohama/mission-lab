# Security

Mission Lab v0.1 is an experimental local research controller. It is not a security sandbox and is not approved for consequential or safety-critical deployment. Use disposable files that you own. The [known issues](KNOWN_ISSUES.md) include unresolved validation, record-authenticity and exceptional cleanup gaps.

## Reporting a vulnerability

If GitHub private vulnerability reporting is enabled for the published repository, use **Security → Report a vulnerability**. Availability of that feature has not been verified for this local release preparation.

If there is no private reporting option, open an issue requesting a private contact route without including exploit details, credentials, personal information or sensitive files. Wait for the maintainer to establish that route before sending sensitive details. No private email address or response-time commitment is currently published.

A useful report identifies the affected revision, a minimal reproduction using synthetic data, the expected boundary and the observed failure. Sanitize filesystem paths, process details and logs before sharing them. Do not upload an entire evidence directory or a private research archive.

Ordinary bugs that do not expose a security boundary can use the public bug report template. Review [KNOWN_ISSUES.md](KNOWN_ISSUES.md) first and identify any overlap.

## Support scope

The current preview is the only maintenance target. There is no supported stable release or security support period yet. Fixes need a reviewed change and relevant verification; an automated review alone does not establish a security guarantee.
