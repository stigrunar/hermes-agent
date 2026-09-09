# Goal helper packaging repair — 2026-09-09

User-authorized bounded repair of the installed goal helper and Python import path. No gateway restart, business data writes, credentials, or Lagerdrift source changes.

## Evidence and correction

Base: `cb84b8366e4d3b6ef67288d10a75bb24a12675cc` (verified origin/main).

Outside the checkout, without PYTHONPATH, the old editable registration (0.20.5) failed to import `hermes_startup_watchdog`, although the file and current pyproject declaration existed. Re-registering the existing 0.21.0 checkout without dependency updates exposed a second missing declaration: `hermes_state_registry`. Add that existing module to setuptools py-modules. No Python runtime behavior changed.

Direct-edit exception: one obvious packaging-list entry, verified by an editable install and fresh-process import. Work performed in isolated `fix/goal-package-registry-20260909` worktree, not active runtime source.

The default-profile `review-and-set-goal` helper now selects a coherent checkout root (explicit HERMES_AGENT_ROOT, runtime HERMES_REPO, or local checkout), preflights SessionDB synchronously, and compares saved goal state before reporting success. This prevents failed database bootstrap/readback from being reported as a successfully saved goal. Its own native skill package is the source surface.

## Verification

- Existing reproduction from `/tmp`, no PYTHONPATH: fails before repair.
- Candidate editable install in isolated test venv: PASS. Test venv reuses parent dependency directory, not parent editable registration.
- Fresh plain imports of hermes_state, hermes_startup_watchdog and hermes_state_registry from `/tmp`, no PYTHONPATH: PASS; registry resolves to exact candidate worktree.
- `python -m pytest tests/hermes_cli/test_goals.py tests/hermes_cli/test_goals_db_bootstrap_off_loop.py -o addopts= -q`: **40 passed**.
- Helper regression checks: independent-process set/readback, replacement protection, invalid database rejection and dropped-write rejection: **4 passed**. All writes used temporary HERMES_HOME; no live goal was set or replaced.
- Standard wheel build is intentionally rejected by this repository's packaging policy; switched to its supported editable-install path. No bypass of wheel guard.

## Bounded installation / rollback

Apply the exact one-line packaging fix to the root installation only after candidate acceptance; refresh editable registration with `python -m pip install --no-deps --no-build-isolation -e <root-install>`. Preserve existing gateway/runtime pins and dependencies. Read back import paths and the goal helper from a fresh process outside the checkout without PYTHONPATH.

Pre-repair editable registration backup: `/home/hermes/.hermes/backups/goal-editable-registration-20260909T202902.tar.gz`. Rollback source to the recorded base only through the normal bounded installation path; the archived registration supplies pre-repair module metadata if needed. Do not reset unrelated work or restart services for this packaging-only fix.
