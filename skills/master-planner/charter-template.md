# Sub-Team Charter Template

## Cardinal rules

State the parent rules this sub-team must preserve without reinterpretation.

```text
CARDINAL_RULES:
- <parent rule or invariant>
```

## Org chart + addressing

Name every seat and record the canonical addresses used for `FROM`, `TO`, and `CC`.

```text
ORG_CHART:
- <seat>: <owner.role>
ADDRESSING:
- FROM: <owner.role>
- TO: <owner.role>
- CC: <owner.role | none>
```

## Domain table with owns/consumes edges

Map each domain to what it owns and the adjacent contracts or artifacts it consumes.

```text
| Domain | Owns | Consumes from |
|---|---|---|
| <domain> | <contract or artifact> | <domain: contract or artifact> |
```

## Layout

Record the sub-team's working-document and relay layout using exact paths.

```text
LAYOUT:
- relay root: <path>
- spec of record: <path>
- ledgers: <paths>
- deliverables: <paths>
```

## Where the rules live

Point to the authoritative protocol, boundary, parent charter, and domain spec locations.

```text
WHERE_THE_RULES_LIVE:
- protocol: <path>
- master-tier boundary: <path>
- parent charter: <path>
- domain spec of record: <path>
```
