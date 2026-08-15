# Documentation contract

This file applies to everything under `docs/`. Read and follow root `../AGENTS.md` first; this file narrows documentation placement and freshness rules without duplicating project policy.

## Canonical root surfaces

- `../README.md` — human entrypoint and navigation.
- `../ROADMAP.md` — decided `Now / Next / Later / Parked` direction.
- `../BACKLOG.md` — unscheduled ideas and review findings.
- `../TASKS.md` — current execution, blockers, and exact next action.
- `../CHANGELOG.md` — delivered, accepted, or released outcomes.
- `../DESIGN.md` — required when product/UI/design is in scope.

## Documentation placement

- `README.md` — documentation map.
- `current/` — current product, architecture, API, operator, and design truth.
- `decisions/` — ADR/STORM rationale.
- `receipts/` — dated verification and acceptance evidence.
- `reports/` — bounded audits, research, and reviews.
- `archive/` — superseded material retained for provenance.

## Freshness invariant

After an authoritative lifecycle transition—accepted design, selected work, material contract change, commit/push/PR/merge, deploy, runtime acceptance/rejection, replacement, or explicit closeout—update the affected root canon and `docs/current/` in the same closeout. Move delivered work out of `ROADMAP.md` `Now` and `TASKS.md` into `CHANGELOG.md`; archive superseded detail instead of deleting provenance.

Do not store secrets, raw private payloads, transient run logs, or duplicated roadmap/task history under `docs/`.
