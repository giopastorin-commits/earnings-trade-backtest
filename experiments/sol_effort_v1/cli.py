"""CLI for DRY RUN, MOCK, and explicitly gated LIVE SHADOW."""

from __future__ import annotations

import argparse
from decimal import Decimal

from .client import MockResponsesClient, OpenAIResponsesClient
from .config import AUTHORIZATION_SENTINEL
from .dataset import load_dataset, select_records
from .archive import load_ledger_dataset
from .runner import run_experiment


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TRINITY Sol effort experiment V1")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", help="historical exported dataset JSON or directory")
    source.add_argument("--ledger", help="read-only path to an archived trinity.sqlite3")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--analytical-commit", default=None)
    parser.add_argument("--output", default="data/sol_effort_v1")
    parser.add_argument("--mode", choices=("dry-run", "mock", "live-shadow"), default="dry-run")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--max-budget-usd", type=Decimal, default=Decimal("0"))
    parser.add_argument("--authorize-live-shadow", default="")
    parser.add_argument("--replicate-id", default="r1")
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dataset = (
        load_ledger_dataset(
            args.ledger,
            run_id=args.run_id,
            analytical_commit=args.analytical_commit,
        )
        if args.ledger
        else load_dataset(args.input)
    )
    records = select_records(dataset, args.limit)
    if args.mode == "dry-run":
        run_experiment(
            records=records,
            dataset=dataset,
            mode=args.mode,
            output_dir=args.output,
            replicate_id=args.replicate_id,
        )
        return 0
    if args.mode == "mock":
        client = MockResponsesClient()
    else:
        if args.authorize_live_shadow != AUTHORIZATION_SENTINEL:
            raise SystemExit(
                "LIVE SHADOW blocked: explicit authorization sentinel required"
            )
        if args.max_budget_usd <= 0:
            raise SystemExit(
                "LIVE SHADOW blocked: --max-budget-usd must be positive"
            )
        client = OpenAIResponsesClient(
            max_budget_usd=args.max_budget_usd,
            authorization=args.authorize_live_shadow,
            max_output_tokens=args.max_output_tokens,
        )
    run_experiment(
        records=records,
        dataset=dataset,
        mode=args.mode,
        output_dir=args.output,
        client=client,
        replicate_id=args.replicate_id,
    )
    return 0

