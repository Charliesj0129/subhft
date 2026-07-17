# Shioaji 1.5.6 Quote-Only Candidate Design

## Goal

Upgrade the platform SDK pin to `shioaji[speed]==1.5.6` and provide a
fail-closed quote-only runtime that subscribes the full configured universe
without consuming an order session or enabling certificate authority (CA)
operations.

The initial production candidate is market-data validation only. It must not
place, modify, or cancel broker orders.

## Evidence

- The configured old-host universe contains 357 symbols.
- Shioaji permits five concurrent sessions and 120 subscriptions per quote
  session.
- Four quote sessions distribute 357 symbols as approximately 90/89/89/89,
  leaving one broker session available for reconnect headroom.
- The official v1.5.6 release adds force-refresh-token login, fixes an HTTP
  server percentage field, and raises Solace reconnect tolerance to 30 seconds.
- The local release tree is an official documentation/plugin snapshot and the
  local Linux artifact is a stripped standalone server. Python API compatibility
  must therefore be proven against the real 1.5.6 wheel and the SDK surface
  harness.

## Runtime Modes

`HFT_ORDER_MODE` accepts the existing simulation and live values plus the new
canonical value `disabled`. Existing aliases are normalized to canonical
values; an unknown value fails at bootstrap instead of silently selecting an
order mode. The legacy `HFT_ORDER_SIMULATION` variable likewise accepts only
explicit true/false tokens.

In `disabled` mode:

- the market-data client is created normally;
- the order client is a role-guarded no-op client whose broker mutations are
  always blocked;
- order login, execution callback registration, startup position recovery, and
  startup fill reconciliation are skipped;
- health readiness requires the market-data login but does not require an order
  login;
- CA activation is false for every market-data facade and the disabled order
  client has no SDK instance.

Order pipeline tasks may remain constructed for platform wiring compatibility,
but no broker order API is reachable because the only order client is the
fail-closed no-op client. Production config must additionally keep strategy
auto-start disabled for the quote validation candidate.

## Live-Order Fail-Closed Rule

When order mode is `live`, `order_client.login()` must return literal success.
A false result or exception leaves the early health endpoint available but
prevents market, strategy, risk, execution, and order services from starting.
Simulation mode retains its existing degraded behavior.

## Market-Data Client Isolation

Bootstrap creates a separate market-data config with `activate_ca=False` for
both single-facade and `QuoteConnectionPool` paths. The pool also overrides
`activate_ca=False` per facade so environment or shared-config drift cannot
activate CA on quote sessions.

## Health and Startup Ordering

The health server starts before any broker login. Readiness is unavailable
during startup, but `/healthz` remains reachable while a broker login blocks or
fails. The later duplicate health-server start is removed.

In quote-only mode, readiness reports the order path as disabled rather than
down. Existing full-engine readiness semantics remain unchanged.

## SDK Upgrade Proof

The 1.5.6 candidate requires:

1. isolated 1.5.6 wheel bootstrap under `/tmp`;
2. a reviewed `surface_1.5.6.json` golden and 1.5.5-to-1.5.6 diff;
3. adapter unit tests and Decimal/scaled-int boundary tests against the real
   1.5.6 wheel;
4. updated `pyproject.toml` and `uv.lock` with runtime version assertion;
5. `make shioaji-guard`, security audit, `make check`, and `make ci`.

No credentialed broker login, old-host deployment, container restart, or live
order operation is included in this implementation phase.

## Rollback

Before deployment, package the exact changed files and retain the old-host
copies. Rollback restores those files and the 1.3.3 image/lock, then follows the
full stop and broker-session release wait. Rollback and deployment each require
fresh user approval.
