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

- ISO week is authoritative. The target is the first actual XNYS session from
  Monday through Sunday of that ISO week, exactly matching frozen
  `observation_policy.slot_target=FIRST_XNYS_SESSION_OF_ISO_WEEK`.
- GitHub registers the 21:00 and 22:00 UTC cron twins on Monday through Friday.
  The gate permits only the target weekday and only the twin mapping to 23:00
  `Europe/Rome`; every other invocation is a no-op.
- Production becomes eligible only after the target session's calendar-derived
  close plus the frozen 20-minute finality grace.
- On a normal week the target is Monday. When Monday is an XNYS holiday the
  target is Tuesday (or the first later XNYS session after a longer closure).
  The scheduler never falls back into the prior ISO week's Friday.
- The scheduled weekday freezes the ISO week and target. Runner delay can
  neither advance nor otherwise change the target; excessive delay fails
  closed instead of retargeting.
- Early closes and Europe/U.S. DST mismatch weeks come from
  `exchange-calendars==4.13.2`; weekday close times are not hardcoded.

V2 completion markers use a new immutable namespace:

`orchestration/weekly/method=TRINITY_WEEKLY_ORCHESTRATOR_V2/...`

The marker is written only for OFFICIAL mode, after the analytical run is
COMPLETE and its immutable archive has succeeded and verified. The archived
OFFICIAL provenance sets `official_benchmark_observation=true`, making the run
eligible for the frozen prospective collector. Telegram remains after marker
commit and OFFICIAL-only. No broker execution path is introduced.

Manual `VALIDATION` `PREFLIGHT_ONLY` and `FULL_SMOKE` remain available with
their V1 target-resolution behavior and can never create an OFFICIAL marker.
