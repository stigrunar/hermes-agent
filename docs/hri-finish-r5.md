# HRI finish R5: managed Codex task scope and legacy deferred claims

Source-only candidate. HRI accepted: false. Installed: false. No integration, push, service/release change or live database write.

## Scope and provenance

Base e970a53730f93eb3816be0ba0de88613784051f3. Direct leaf execution ex_29368028 used gpt-5.6-luna/xhigh with delegation disabled. It reached its 600-second boundary without a final response, turn.completed or handoff. Its execution remains failed; this document is an explicit owner recovery, not a fabricated worker completion. Owner ex_34e23db5 verified that no Codex process survived and recovered the exact five-file source/test manifest.

- R5-C1: apply the source correction and regression suite from upstream PR107337, head593cbe2838e347cb94dea42c577a0a37aa21af39. Worker overrides now target managed hermes-tools, matching migration, while user-defined hermes-mcp remains untouched.
- R5-C2: preserve merged upstream PR105003/b578261584ef20720938689d217e3793e5b96d84: native executor descendants are denied task authority; only the managed tool endpoint receives explicit scope. Reframe the contradictory older PR81843 assertion with negative task-authority, positive read-routing/credential, and positive managed-endpoint coverage. No delegation_context.py or env-builder production changes.
- R5-C3: deferred queries and in-flight serialization treat legacy NULL adapter_profile as default, without admitting named profiles. Successful guarded claims normalize the legacy row. Owner PID/start, CAS, not-before, attempts and cancellation safeguards remain.

## Actual verification

Native worker evidence: 16 managed MCP cases passed; combined three-file run was incomplete because the ledger file hit the 90-second sandbox limit. Compilation and Ruff passed. This is not a green combined worker gate.

Owner host-side canonical proof on identical recovered bytes:
`bash scripts/run_tests.sh -j 2 --file-timeout 90 --file-retries 0 tests/agent/transports/test_codex_managed_mcp_overrides.py tests/tools/test_hermes_subprocess_env.py tests/gateway/test_delivery_ledger.py tests/tools/test_delegate_kanban_isolation.py tests/tools/test_kanban_descendant_scope.py tests/tools/test_local_env_blocklist.py`

Result: six files, 192 passed, 0 failed, 3 skipped; 22.5 seconds test runtime, source stable. The ledger file passes outside the sandbox without increasing its timeout. Evidence: /home/hermes/exports/hri-finish-r5-owner/affected-result.json, affected.log and affected-command.json. Recovered source hashes: /home/hermes/exports/hri-finish-r5/owner-recovered-manifest.json.

## Remaining gates

The exact checkpoint must pass the original 108-file preservation set plus the new PR regression file (109 unique files), fresh independent read-only review, non-force private integration, immutable release/rollback preparation and actual-target source/PID/health/functional and update-idempotence proof. Source or process completion alone is not HRI release acceptance.
