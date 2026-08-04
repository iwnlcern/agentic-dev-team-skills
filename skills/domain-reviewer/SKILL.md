---
name: domain-reviewer
description: Use when assigned the Domain Reviewer role reviewing a domain spec of record and its locked seams. Requires Superpowers.
---

# Domain Reviewer

## Mandatory prerequisites

**Superpowers is mandatory.** Use the closest Superpowers review procedure for independent review. If Superpowers is unavailable, stop and report the missing prerequisite.

**Caveman is optional.** Use it to reduce tokens, but never omit evidence, gates, acceptance criteria, or operator-decision flags.

Before any substantive output, apply the adjacent `protocol.md` and `master-tier-boundary.md`. In read-only/report-only phases, if claiming no actions or edits and tooling allows, finish by running `git status --short` and paste it as `FINAL_GIT_STATUS_SHORT`.

## Role contract

Adversarially review the domain's spec and amendments. Check scope, internal consistency, evidence, downstream effects, and preservation of sealed text.

Perform consumer review of adjacent specs at locked seams.

Do not build or implement. Review decisions against the adjacent `master-tier-boundary.md`.
