# INDEX — v29
COMMISSIONED_BY: {"commissioning_path":"parent/IMPL-planner-20260808-000000.md","dispatch_content_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","parent_root_uuid":"123e4567-e89b-12d3-a456-426614174000","record_digest":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}

| time | phase | role | dispatch | parent | from | to | cc | status | file |
|---|---|---|---|---|---|---|---|---|---|
| 20260808-010000 | PLAN | Planner | d1 | — | v29-a.planner | v29-a.implementer | — | active | lane/one.md |
| 20260808-010001 | PLAN-REVIEW | Implementer | d2 | d1 | v29-a.implementer | v29-a.planner | v29-b.implementer | superseded | lane/two.md |
| 20260808-010002 | PLAN | Planner | d3 | d2 | v29-a.planner | v29-a.implementer, v29-b.implementer | — | needs\|review | lane/three.md |
| 20260808-010003 | IMPL | Implementer | d4 | d3 | v29-a.implementer | v29-a.planner | — | active | lane/four.md |
| 20260808-010004 | REVIEW | Implementer | d5 | d4 | v29-b.implementer | v29-a.planner | v29-a.implementer | done | lane/five.md |
| 20260808-010005 | SITREP | Planner | d6 | — | v29.orchestrator-planner | v29-a.planner | — | — | lane/six.md |
