# TRINITY Sol Effort Comparison V1

This is an isolated, non-production experiment. It does not modify Scanner,
Luna, Sol, Critic, official workflows, R2, or the frozen V1 protocol.

The factorial arms are:

- A: Analyst medium, Critic medium
- B: Analyst medium, Critic high
- C: Analyst high, Critic medium
- D: Analyst high, Critic high

Each selected record produces exactly six planned calls: two Analyst calls
and four Critic calls. The two Critic calls for a given Analyst draft reuse
the exact same draft text and record its hash.

## Input contract

The loader accepts a JSON file or a directory containing
sol_effort_dataset.json, records.json, or dataset.json. The dataset must
contain exported point-in-time prompts, schemas, facts, sources, and a cutoff.
Sources dated after the cutoff are rejected. The included fixture is a test
fixture, not historical evidence.

For a real frozen Ledger archive, the read-only SQLite adapter can be used
directly by the CLI:

    python -m experiments.sol_effort_v1 --ledger path/to/trinity.sqlite3 --run-id RUN_ID --limit 3 --mode dry-run --output data/sol_effort_v1-ledger-dry

The adapter requires a verifiable historical Analyst/Critic lineage and
rejects records without a usable cutoff or an exact Analyst draft in the
historical Critic prompt.

## Commands

From the repository root:

    python -m experiments.sol_effort_v1 --input experiments/sol_effort_v1/fixtures/sample_dataset.json --limit 3 --mode dry-run --output data/sol_effort_v1-dry

    python -m experiments.sol_effort_v1 --input experiments/sol_effort_v1/fixtures/sample_dataset.json --limit 3 --mode mock --output data/sol_effort_v1-mock

The default mode is dry-run. It performs no model call. Mock is fully offline
and deterministic. Live-shadow requires the authorization sentinel, a
positive budget, and OPENAI_API_KEY; it is never the default.

The experiment scorecard is separate from the official scorecard. Financial
outcomes remain unavailable until COMPLETE point-in-time observations are
provided; no returns are inferred or synthesized.

