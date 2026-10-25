# TRINITY Weekly Orchestrator V2

Status: registered before the first OFFICIAL observation.

`TRINITY_WEEKLY_ORCHESTRATOR_V2` replaces the V1 operational timing contract
for future OFFICIAL runs. V1 is not overwritten. The frozen analytical ref
remains `eb15c45b041b8890194d2f98b025aa98ed7fa527`.

This is an orchestrator-method amendment, not a new live-validation analytical
epoch. `TRINITY_LIVE_VALIDATION_V1`, epoch 1, remains frozen because selection
cohorts, Scanner/Luna/Sol behavior, benchmark and SimpleQuant methods,
horizons, costs, ranking, trade plans, and statistical endpoints are unchanged.

## OFFICIAL decision contract

- The intended weekly decision instant is Monday 23:00 `Europe/Rome`.
- GitHub registers the 21:00 and 22:00 UTC Monday cron twins. The gate selects
  only the twin that maps to 23:00 Rome for that date; the other is a no-op.
- The target is the latest XNYS session whose calendar-derived close plus the
  frozen 20-minute finality grace is complete at the intended decision instant.
- Runner delay cannot advance the target because resolution uses the intended
  decision instant, not the runner's eventual wall clock.
- On a normal Monday the target is Monday. On a Monday XNYS holiday the target
  is the latest prior final session. Early closes and Europe/U.S. DST mismatch
  weeks come from `exchange-calendars==4.13.2`; weekday close times are not
  hardcoded.

V2 completion markers use a new immutable namespace:

`orchestration/weekly/method=TRINITY_WEEKLY_ORCHESTRATOR_V2/...`

The marker is written only for OFFICIAL mode, after the analytical run is
COMPLETE and its immutable archive has succeeded and verified. The archived
OFFICIAL provenance sets `official_benchmark_observation=true`, making the run
eligible for the frozen prospective collector. Telegram remains after marker
commit and OFFICIAL-only. No broker execution path is introduced.

Manual `VALIDATION` `PREFLIGHT_ONLY` and `FULL_SMOKE` remain available with
their V1 target-resolution behavior and can never create an OFFICIAL marker.
