# ADR-0001: Record Architecture Decisions

## Status

Accepted — 2026-05-06

## Context

Architecture decisions on this project will be made repeatedly: tool surface,
trajectory schema, sandbox lifecycle, judge calibration, configuration
representation, etc. Decisions made in chat, in commit messages, or in PR
descriptions are hard to find later — both for current contributors who need
to remember *why* something is a certain way, and for future readers
(including reviewers) who want to understand the reasoning behind the code.

## Decision

We will record significant architectural decisions as ADRs in
`docs/adr/<NNNN>-<short-kebab-title>.md`, following the template established
by [Michael Nygard][nygard]. Each ADR has:

- **Title** with sequential number
- **Status** — one of: Proposed, Accepted, Deprecated, Superseded by ADR-XXXX
- **Context** — what problem we're addressing and what we know about it
- **Decision** — what we're doing about it
- **Consequences** — what becomes easier and harder because of this decision

ADRs are append-only. We do not edit accepted ADRs to change their meaning;
if a decision changes, we write a new ADR that supersedes the old one and
update the old one's status accordingly.

[nygard]: https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions

## Consequences

**Easier:**

- Future contributors can answer "why is this like this?" by reading ADRs
  rather than excavating Git history.
- Reviewers can audit the reasoning behind significant decisions without
  asking the author.
- Decisions become explicit artifacts, which makes deliberation more
  rigorous (it's harder to handwave when you have to write it down).

**Harder:**

- Adds ~30 minutes per significant decision.
- Requires discipline to remember to write the ADR rather than letting the
  reasoning live in a commit message or chat log.
- Risk of bikeshedding what counts as "significant." Heuristic: if you'd
  want to explain it in an interview, it's significant.

## Notes

- Trivial decisions (file naming, formatter rules) live in commit messages,
  not ADRs.
- ADRs are not required for every PR — most PRs implement decisions made in
  earlier ADRs.
- When superseding an ADR, update the old one's status to
  `Superseded by ADR-XXXX` but otherwise leave it intact.

## References

- Michael Nygard, *Documenting Architecture Decisions*, 2011.
- [adr-tools][adr-tools] — optional CLI for managing ADRs.

[adr-tools]: https://github.com/npryce/adr-tools
