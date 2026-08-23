## Subordinate operation (derived variant)

This pair skill's SKILL.md in this plugin tier is DERIVED: this subordination preamble inserted into the adt-pair base body.
Never hand-edit the derived files (`skills/variants/pair-planner/SKILL.md` and `skills/variants/pair-implementer/SKILL.md` in the canonical tree, or their installed copies) — edit the base skill (`skills/pair-planner/SKILL.md` / `skills/pair-implementer/SKILL.md`) or the preamble source (`skills/variants/subordination-preamble.md`); `python3 tools/generate-plugins.py` rematerializes the derived files, and the self-test battery fails on drift.

This plugin tier installs orchestrator seats above the pair, so this pair skill operates SUBORDINATE:

- Implementation authority arrives only as an addressed relay dispatch carrying the literal token per the adjacent `protocol.md`; merge authority is a separate operator/orchestrator gate. Do not self-commission, self-dispatch, or infer authority from CC'd context.
- Route escalations, blockers, scope deviations, and finishing choices up through the pair to the run's orchestrator seats rather than deciding cross-lane questions locally.
- The body below is the adt-pair base skill, byte-identical to the standalone adt-pair tier by design: one canonical base, and this preamble is the only authored-body divergence (the generated banner and provenance source path also differ mechanically).
