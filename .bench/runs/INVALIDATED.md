# Invalidated harness runs

These artifacts are retained only as QA-runner debugging evidence and must not
be used as DeckPipe baselines:

- `installed-20260826-181331.*` — the first normal-size UIA snapshot was taken
  while the WebView accessibility tree was transient (17 nodes). The runner
  was fixed with a readiness gate and regression test.
- `isolated-ui-20260826-182000.*` — empty JSON arrays were collapsed to `null`
  and failed aggregate calculation. The runner was fixed with an explicit
  empty-collection contract.

Canonical installed baseline: `../baseline-installed-0.5.0.json`.
