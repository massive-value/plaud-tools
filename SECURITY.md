# Security Policy

## Reporting a Vulnerability

If you believe you have found a security vulnerability in `plaud-tools`, please
report it privately by email to **dev.bluehorizon@gmail.com**. Please do not
open a public GitHub issue for security reports.

When reporting, include as much of the following as you can:

- A description of the vulnerability and the impact you believe it has.
- Step-by-step reproduction instructions, including the affected version
  (`plaud-tools --version`) and operating system.
- Any proof-of-concept code, logs, or captures that help demonstrate the issue.
- Whether you would like to be credited in the changelog when a fix ships.

## Response Expectations

`plaud-tools` is maintained by a single volunteer in their spare time. There
is **no service-level agreement** on response or remediation times. Reports
are triaged on a best-effort basis. You will normally receive an
acknowledgement within a few weeks; complex fixes may take longer.

If your report is accepted and a fix is released, the maintainer will note the
vulnerability in `CHANGELOG.md` and, with your consent, credit you.

## Scope

The following areas of `plaud-tools` are in scope for security reports:

- **Token handling** — how Plaud auth tokens are obtained, stored, refreshed,
  and transmitted.
- **Session storage** — the on-disk session file, keyring entries, and any
  cached credentials.
- **Transcode subprocess** — invocations of `ffmpeg` and other external
  binaries, especially command-injection or path-traversal risks.
- **Auth flow** — the login wizard, MFA handling, and any code paths that
  prompt the user for credentials.

## Out of Scope

The following are explicitly **out of scope** for this project's security
policy:

- Vulnerabilities in the upstream Plaud API or the Plaud Note service itself —
  please report those to Plaud directly.
- Issues that require physical access to an unlocked machine already running
  `plaud-tools` as the authenticated user.
- Bugs in third-party dependencies that are not exploitable through
  `plaud-tools`' use of them; please report those upstream.
- Denial-of-service against your own Plaud account or device through normal
  API use.

Thank you for helping keep `plaud-tools` and its users safe.
