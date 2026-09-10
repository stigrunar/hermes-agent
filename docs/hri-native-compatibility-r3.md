# HRI native compatibility R3 source correction

Project `p_155df2bb`, Outcome `o_cde72dc3`; base `ae6dc7510e8aa2ae56152409074baff3bc7c0371`.

## Implemented

- `tui_gateway.method_ctx.bind_module` now treats the exact anonymous handler name `_` as
  registry plumbing. Native `HandlerRegistry` installation still registers each JSON-RPC name,
  while named split-module helpers retain collision detection and rebinding behavior.
- Added focused synthetic-module coverage for two anonymous handlers and for a named-helper
  collision that must raise without replacing the first helper.
- Restored the missing `SimpleNamespace` import in the gateway loop watcher fixture.
- Corrected the corrupt-board fixture to expect three connections for either review flag: the
  dispatcher opens one connection per board and shares it across ready/review predicates, so a
  corrupt connect aborts both together.

## Local proof

- Changed Python files compile successfully with `python -m compileall`.
- `git diff --check` passes.
- Owner pytest acceptance remains pending; no pytest, service, activation, or live database run was
  performed in this leaf.

This is an uncommitted internal source correction. It is not installed and does not claim HRI or
release acceptance.
