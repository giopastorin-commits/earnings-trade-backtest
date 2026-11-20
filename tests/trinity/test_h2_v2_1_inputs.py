from __future__ import annotations

from datetime import date, datetime, timezone
import csv
import hashlib
import inspect
import json
from pathlib import Path

import pytest

import trinity.h2_v2_1_inputs as phase1
from trinity.h2_v2_1_inputs import (
    EXCLUSION_COLUMNS,
    INPUT_COLUMNS,
    PriceCoverageError,
    assign_sample,
    build_phase1_inputs,
    canonical_json_bytes,
    classify_and_resolve,
    companyfacts_fiscal_tuple,
    descriptive_release_period,
    file_inventory,
    iter_bars_through_reference,
    latest_periodic_before,
    ordered_bar_bytes,
    require_complete_price_window,
    select_ex99,
    successor_fiscal_period,
    write_phase1_csvs,
)
from trinity.outcomes import EventTiming, _Bar, _resolve_reference


UTC = timezone.utc


def _bar(day: date) -> _Bar:
    return _Bar(day, 1.0, 1.0, 1.0, 1.0, 1.0)


@pytest.mark.parametrize(
    ("accepted", "timing", "reference", "resolution"),
    [
        ("2026-09-21T13:29:59Z", "PRE_MARKET", date(2026, 9, 18), "PREVIOUS_SESSION_CLOSE"),
        ("2026-09-21T13:30:00Z", "INTRADAY", None, "AMBIGUOUS"),
        ("2026-09-21T19:59:59Z", "INTRADAY", None, "AMBIGUOUS"),
        ("2026-09-21T20:00:00Z", "AFTER_MARKET", date(2026, 9, 21), "EVENT_SESSION_CLOSE"),
        (
            "2026-10-24T12:00:00Z", "PRE_MARKET", date(2026, 10, 23),
            "LAST_SESSION_BEFORE_NON_TRADING_EVENT",
        ),
    ],
)
def test_timing_boundaries_and_non_session(accepted, timing, reference, resolution):
    result = classify_and_resolve(accepted)
    assert result.timing == timing
    assert result.reference_session == reference
    assert result.reference_resolution == resolution
    assert result.ambiguous is (timing in {"INTRADAY", "UNKNOWN"})


def test_unknown_timestamp_is_unassigned_and_descriptive_only():
    result = classify_and_resolve("not-a-timestamp")
    assert result.timing == "UNKNOWN"
    assert assign_sample(datetime(2026, 11, 1, tzinfo=UTC), result) == "UNASSIGNED"
    assert descriptive_release_period(None) == "OTHER"


def test_release_period_does_not_assign_sample():
    intraday = classify_and_resolve("2026-10-21T15:00:00Z")
    assert descriptive_release_period(intraday.local_date) == "PROSP_PERIOD"
    assert assign_sample(datetime(2026, 10, 21, 15, tzinfo=UTC), intraday) == "UNASSIGNED"


@pytest.mark.parametrize(
    ("event_day", "timing", "bar_days"),
    [
        (date(2026, 10, 21), EventTiming.PRE_MARKET, [date(2026, 10, 20), date(2026, 10, 21)]),
        (date(2026, 10, 21), EventTiming.AFTER_MARKET, [date(2026, 10, 20), date(2026, 10, 21)]),
        (date(2026, 10, 24), EventTiming.PRE_MARKET, [date(2026, 10, 23), date(2026, 10, 26)]),
        (date(2026, 11, 26), EventTiming.PRE_MARKET, [date(2026, 11, 25), date(2026, 11, 27)]),
    ],
)
def test_calendar_reference_matches_outcome_engine_on_synthetic_bars(
    event_day, timing, bar_days,
):
    calendar_result = classify_and_resolve(
        datetime.combine(
            event_day,
            datetime.min.time().replace(hour=8 if timing is EventTiming.PRE_MARKET else 17),
            tzinfo=phase1.NY,
        ).astimezone(UTC).isoformat(),
    )
    index, _label = _resolve_reference([_bar(day) for day in bar_days], event_day, timing)
    assert calendar_result.reference_session == bar_days[index]


def _companyfacts(accession="0001-26-000001", fy=2026, fp="Q1", end="2026-03-31"):
    return {
        "facts": {
            "us-gaap": {
                "Revenue": {
                    "units": {
                        "USD": [{
                            "accn": accession, "form": "10-Q", "end": end,
                            "fy": fy, "fp": fp,
                        }]
                    }
                }
            }
        }
    }


@pytest.mark.parametrize(
    ("fp", "fy", "expected"),
    [
        ("Q1", 2026, (2026, 2, "QUARTERLY", "Q2")),
        ("Q2", 2026, (2026, 3, "QUARTERLY", "Q3")),
        ("Q3", 2026, (2026, 4, "QUARTERLY", "FY/Q4")),
        ("FY", 2026, (2027, 1, "QUARTERLY", "Q1")),
    ],
)
def test_fiscal_successor_rules(fp, fy, expected):
    filing = {
        "accessionNumber": "0001-26-000001", "form": "10-Q" if fp != "FY" else "10-K",
        "reportDate": "2026-03-31", "acceptanceDateTime": "2026-05-01T12:00:00Z",
    }
    facts = _companyfacts(fy=fy, fp=fp)
    if fp == "FY":
        facts["facts"]["us-gaap"]["Revenue"]["units"]["USD"][0]["form"] = "10-K"
    result = successor_fiscal_period(filing, facts)
    assert (result.fiscal_year, result.fiscal_quarter, result.period_type, result.label) == expected


def test_fiscal_metadata_is_null_when_companyfacts_is_ambiguous():
    facts = _companyfacts()
    facts["facts"]["us-gaap"]["Assets"] = {
        "units": {"USD": [{
            "accn": "0001-26-000001", "form": "10-Q", "end": "2026-03-31",
            "fy": 2026, "fp": "Q2",
        }]}
    }
    assert companyfacts_fiscal_tuple(
        facts, accession="0001-26-000001", report_date="2026-03-31",
    ) is None
    result = successor_fiscal_period({
        "accessionNumber": "0001-26-000001", "form": "10-Q",
        "reportDate": "2026-03-31", "acceptanceDateTime": "2026-05-01T12:00:00Z",
    }, facts)
    assert result.period_type == "UNKNOWN"
    assert result.fiscal_year is None


def test_latest_periodic_is_strictly_before_release():
    release = datetime(2026, 5, 1, 12, tzinfo=UTC)
    filings = [
        {"form": "10-Q", "acceptanceDateTime": "2026-04-01T12:00:00Z", "id": "old"},
        {"form": "10-K/A", "acceptanceDateTime": "2026-04-15T12:00:00Z", "id": "amended"},
        {"form": "10-Q", "acceptanceDateTime": "2026-05-01T12:00:00Z", "id": "equal"},
    ]
    assert latest_periodic_before(filings, release)["id"] == "old"


def test_ex99_1_is_preferred_over_document_order():
    submission = b"""
<DOCUMENT><TYPE>EX-99.2\n<FILENAME>second.htm\n<TEXT><p>second</p></TEXT></DOCUMENT>
<DOCUMENT><TYPE>EX-99.1\n<FILENAME>preferred.htm\n<TEXT><p>preferred</p></TEXT></DOCUMENT>
"""
    selected = select_ex99(submission)
    assert selected is not None
    assert (selected.document_type, selected.filename, selected.ordinal) == (
        "EX-99.1", "preferred.htm", 2,
    )


def test_first_ex99_is_used_without_exact_ex99_1():
    submission = b"""
<DOCUMENT><TYPE>EX-99.02\n<FILENAME>first.htm\n<TEXT><p>first</p></TEXT></DOCUMENT>
<DOCUMENT><TYPE>EX-99.3\n<FILENAME>later.htm\n<TEXT><p>later</p></TEXT></DOCUMENT>
"""
    selected = select_ex99(submission)
    assert selected is not None
    assert selected.filename == "first.htm"


class _SentinelLines:
    def __init__(self, before_and_reference):
        self.before_and_reference = before_and_reference

    def __iter__(self):
        yield from self.before_and_reference
        raise AssertionError("post-reference line was requested")


def _price_line(day: str, close: float) -> bytes:
    return ordered_bar_bytes({
        "date": day, "open": close, "high": close, "low": close,
        "close": close, "volume": 10.0,
    })


def test_price_reader_never_requests_post_reference_line():
    lines = _SentinelLines([
        _price_line("2026-10-19", 10.0),
        _price_line("2026-10-20", 11.0),
    ])
    result = iter_bars_through_reference(lines, date(2026, 10, 20))
    assert [row["date"] for row in result] == ["2026-10-19", "2026-10-20"]


def test_post_reference_payload_cannot_change_bounded_result():
    prefix = [_price_line("2026-10-19", 10.0), _price_line("2026-10-20", 11.0)]
    first = iter_bars_through_reference(
        [*prefix, _price_line("2026-10-21", 999.0)], date(2026, 10, 20),
    )
    second = iter_bars_through_reference(
        [*prefix, _price_line("2026-10-21", 0.01)], date(2026, 10, 20),
    )
    assert first == second


def test_missing_reference_is_price_coverage():
    with pytest.raises(PriceCoverageError, match="reference session"):
        iter_bars_through_reference(
            [_price_line("2026-10-19", 10.0), _price_line("2026-10-21", 11.0)],
            date(2026, 10, 20),
        )


def test_price_coverage_requires_61_consecutive_xnys_sessions():
    calendar = phase1.xnys_calendar()
    sessions = [
        item.date()
        for item in calendar.sessions_window("2026-10-20", -61)
    ]
    bars = [{"date": day.isoformat()} for day in sessions]
    require_complete_price_window(bars, date(2026, 10, 20), calendar=calendar)
    with pytest.raises(PriceCoverageError, match="consecutive XNYS"):
        require_complete_price_window(
            [{"date": "2026-01-01"}, *bars[1:]],
            date(2026, 10, 20), calendar=calendar,
        )


def test_deterministic_csv_output_under_shuffled_input(tmp_path):
    rows = [
        {"sample": "DEV", "current_release_id": "b", "ticker": "B",
         "release_at": "2026-10-01T12:00:00Z", "reference_session_date": "2026-09-30"},
        {"sample": "DEV", "current_release_id": "a", "ticker": "A",
         "release_at": "2026-09-22T12:00:00Z", "reference_session_date": "2026-09-21"},
    ]
    exclusions = [{
        "sample": "UNASSIGNED", "ticker": "C", "current_release_id": "c",
        "release_at": "2026-10-21T15:00:00Z", "reason": "TIMING_INTRADAY",
    }]
    first = write_phase1_csvs(tmp_path / "one", rows, [], exclusions)
    second = write_phase1_csvs(tmp_path / "two", list(reversed(rows)), [], exclusions)
    assert first == second
    for name in ("h2_v2_1_inputs_dev.csv", "h2_v2_1_inputs_prosp_batch1.csv",
                 "h2_v2_1_exclusions.csv"):
        assert (tmp_path / "one" / name).read_bytes() == (tmp_path / "two" / name).read_bytes()


def test_phase1_schema_and_module_have_no_outcomes_or_predictions():
    forbidden_columns = {
        "absolute_return_pct_20", "raw_return_pct_20", "excess_return_pct_20",
        "outcome_status_20", "pred_0", "pred_1",
    }
    assert forbidden_columns.isdisjoint(INPUT_COLUMNS)
    assert "reason" in EXCLUSION_COLUMNS
    source = inspect.getsource(phase1)
    assert "calculate_event_outcomes" not in source
    assert "build_experimental_dataset" not in source


def test_complete_submission_transient_failure_is_deferred_then_required():
    class StubClient:
        def __init__(self):
            self.calls = 0

        def get(self, url, relative_path):
            self.calls += 1
            if self.calls == 1:
                raise phase1.H2Phase1Error("transient")
            return b"complete submission"

    client = StubClient()
    deferred = []
    relative_path = Path("complete_submissions/TST/0001.txt")
    phase1.cache_complete_submission_or_defer(
        client, "https://www.sec.gov/example.txt", relative_path, deferred,
    )
    assert deferred == [("https://www.sec.gov/example.txt", relative_path)]
    phase1.retry_deferred_complete_submissions(client, deferred)
    assert client.calls == 2


def test_unavailable_deferred_submission_prevents_corpus_seal():
    class FailingClient:
        def get(self, url, relative_path):
            raise phase1.H2Phase1Error("still unavailable")

    with pytest.raises(phase1.H2Phase1Error, match="still unavailable"):
        phase1.retry_deferred_complete_submissions(
            FailingClient(), [("https://www.sec.gov/example.txt", Path("missing.txt"))],
        )


def test_share_class_resolution_uses_first_supported_sec_ticker(tmp_path):
    sec_root = tmp_path / "sec"
    cik = "0000000001"
    tickers = ["BETA-SEC", "ALPHA-SEC", "NOT_SUPPORTED"]
    issuers = []
    for ticker in ("ALPHA", "BETA"):
        path = sec_root / "cache" / "submissions" / ticker / f"CIK{cik}.json"
        path.parent.mkdir(parents=True)
        body = canonical_json_bytes({"tickers": tickers}, newline=True)
        path.write_bytes(body)
        path.with_suffix(".json.meta.json").write_bytes(canonical_json_bytes({
            "sha256": hashlib.sha256(body).hexdigest(),
            "retrieved_at_utc": f"2026-11-19T12:00:0{len(issuers)}Z",
        }, newline=True))
        issuers.append({"ticker": ticker, "cik": cik, "filings": []})

    plans, audit = phase1.resolve_share_class_plans(
        sec_root, issuers, {"ALPHA": cik, "BETA": cik},
        {"ALPHA-SEC": "ALPHA", "BETA-SEC": "BETA"},
    )
    assert len(plans) == 1
    assert plans[0].selected_sec_ticker == "BETA-SEC"
    assert plans[0].selected_ticker == "BETA"
    assert plans[0].resolution_method == "SEC_ORDER"
    assert plans[0].resolution_reason is None
    assert plans[0].nonselected_supported_tickers == ("ALPHA",)
    assert audit["cases"] == [{
        "cik": cik,
        "sec_tickers": tickers,
        "registry_supported_tickers": ["ALPHA", "BETA"],
        "resolution_method": "SEC_ORDER",
        "reason": None,
        "selected_sec_ticker": "BETA-SEC",
        "selected_ticker": "BETA",
        "registry_ticker_used": "BETA",
        "nonselected_supported_tickers": ["ALPHA"],
        "submissions_source_relative_path": "cache/submissions/BETA/CIK0000000001.json",
        "submissions_sha256": hashlib.sha256(body).hexdigest(),
        "acquired_at_utc": "2026-11-19T12:00:01Z",
    }]


@pytest.mark.parametrize(
    ("sec_tickers", "reason"),
    [([], "SEC_TICKERS_EMPTY"), (["OLD", "OLD-WT"], "SEC_TICKERS_NO_REGISTRY_MATCH")],
)
def test_unique_cik_registry_fallback_is_audited(tmp_path, sec_tickers, reason):
    sec_root = tmp_path / "sec"
    cik = "0000000001"
    path = sec_root / "cache" / "submissions" / "ONLY" / f"CIK{cik}.json"
    path.parent.mkdir(parents=True)
    body = canonical_json_bytes({"tickers": sec_tickers}, newline=True)
    path.write_bytes(body)
    path.with_suffix(".json.meta.json").write_bytes(canonical_json_bytes({
        "sha256": hashlib.sha256(body).hexdigest(),
        "retrieved_at_utc": "2026-11-19T12:00:00Z",
    }, newline=True))
    plans, audit = phase1.resolve_share_class_plans(
        sec_root, [{"ticker": "ONLY", "cik": cik, "filings": []}],
        {"ONLY": cik}, {"ONLY-SEC": "ONLY"},
    )
    assert plans[0].selected_ticker == "ONLY"
    assert plans[0].selected_sec_ticker is None
    assert plans[0].resolution_method == "REGISTRY_UNIQUE_CIK_FALLBACK"
    assert plans[0].resolution_reason == reason
    assert audit["cases"][0]["sec_tickers"] == sec_tickers
    assert audit["cases"][0]["registry_ticker_used"] == "ONLY"
    assert audit["cases"][0]["reason"] == reason


def test_unmatched_sec_tickers_with_multiple_registry_records_are_ambiguous(tmp_path):
    sec_root = tmp_path / "sec"
    cik = "0000000001"
    issuers = []
    for ticker in ("ONE", "TWO"):
        path = sec_root / "cache" / "submissions" / ticker / f"CIK{cik}.json"
        path.parent.mkdir(parents=True)
        body = canonical_json_bytes({"tickers": ["UNMATCHED"]}, newline=True)
        path.write_bytes(body)
        path.with_suffix(".json.meta.json").write_bytes(canonical_json_bytes({
            "sha256": hashlib.sha256(body).hexdigest(),
            "retrieved_at_utc": "2026-11-19T12:00:00Z",
        }, newline=True))
        issuers.append({"ticker": ticker, "cik": cik, "filings": []})
    plans, audit = phase1.resolve_share_class_plans(
        sec_root, issuers, {"ONE": cik, "TWO": cik},
        {"ONE-SEC": "ONE", "TWO-SEC": "TWO"},
    )
    assert plans[0].selected_ticker is None
    assert plans[0].resolution_method == "AMBIGUOUS_TICKER_MAPPING"
    assert plans[0].resolution_reason == "SEC_TICKERS_NO_REGISTRY_MATCH"
    assert audit["cases"][0]["registry_ticker_used"] is None


def test_ambiguous_ticker_mapping_is_excluded_without_analytical_inputs(tmp_path):
    sec_root = tmp_path / "sec"
    cache = sec_root / "cache"
    cik = "0000000001"
    filings = [{
        "accessionNumber": "0001-26-000001", "form": "8-K", "items": "2.02,9.01",
        "acceptanceDateTime": "2026-10-01T20:30:00Z",
    }]
    issuers = []
    for ticker in ("ONE", "TWO"):
        path = cache / "submissions" / ticker / f"CIK{cik}.json"
        path.parent.mkdir(parents=True)
        body = canonical_json_bytes({"tickers": ["UNMATCHED"]}, newline=True)
        path.write_bytes(body)
        path.with_suffix(".json.meta.json").write_bytes(canonical_json_bytes({
            "sha256": hashlib.sha256(body).hexdigest(),
            "retrieved_at_utc": "2026-11-19T12:00:00Z",
        }, newline=True))
        issuers.append({"ticker": ticker, "cik": cik, "filings": filings})
    index_path = sec_root / "sec_corpus_index.json"
    index_path.write_bytes(canonical_json_bytes({"issuers": issuers}, newline=True))
    sec_files = file_inventory(sec_root, exclude_names=frozenset({"sec_acquisition_manifest.json"}))
    (sec_root / "sec_acquisition_manifest.json").write_bytes(canonical_json_bytes({
        "schema_name": "trinity.h2-v2-1-sec-acquisition",
        "index_sha256": hashlib.sha256(index_path.read_bytes()).hexdigest(),
        "files": sec_files,
        "ensemble_sha256": hashlib.sha256(canonical_json_bytes(sec_files)).hexdigest(),
    }, newline=True))

    price_root = tmp_path / "prices"
    price_root.mkdir()
    price_files = file_inventory(price_root, exclude_names=frozenset({"price_ensemble_manifest.json"}))
    (price_root / "price_ensemble_manifest.json").write_bytes(canonical_json_bytes({
        "schema_name": "trinity.h2-v2-1-price-ensemble",
        "schema_version": "1", "spec_id": phase1.SPEC_ID,
        "provider": "SYNTHETIC", "snapshot_id": "SYNTHETIC",
        "issuer_snapshot_sha256": "a" * 64, "target_session": "2026-11-17",
        "acquisition_started_at_utc": "synthetic",
        "acquisition_completed_at_utc": "synthetic", "endpoint": "synthetic",
        "parameters": {}, "adjustment_policy": {},
        "spy_normalized_sha256": "b" * 64,
        "files": price_files,
        "ensemble_sha256": hashlib.sha256(canonical_json_bytes(price_files)).hexdigest(),
    }, newline=True))

    output = tmp_path / "output"
    text_root = tmp_path / "texts"
    result = build_phase1_inputs(
        repo_root=Path(phase1.__file__).resolve().parents[1], sec_root=sec_root,
        price_root=price_root, text_root=text_root, output_root=output,
        supported_by_ticker={"ONE": cik, "TWO": cik},
        sec_ticker_to_canonical={"ONE-SEC": "ONE", "TWO-SEC": "TWO"},
    )
    with (output / "h2_v2_1_exclusions.csv").open(encoding="utf-8", newline="") as handle:
        exclusions = list(csv.DictReader(handle))
    assert len(exclusions) == 1
    assert exclusions[0]["reason"] == "AMBIGUOUS_TICKER_MAPPING"
    assert exclusions[0]["ticker"] == ""
    assert not any(text_root.rglob("*.txt"))
    resolution = json.loads(
        (output / "h2_v2_1_share_class_resolution.json").read_text(encoding="utf-8")
    )
    assert resolution["cases"][0]["event_counts"]["DEV"] == 1
    assert result["dev_candidate_count"] == 0


def test_synthetic_end_to_end_build_is_outcome_free(tmp_path):
    sec_root = tmp_path / "sec"
    cache = sec_root / "cache"
    for ticker in ("TST", "TSTB"):
        submissions_path = cache / "submissions" / ticker / "CIK0000000001.json"
        submissions_path.parent.mkdir(parents=True)
        submissions_body = canonical_json_bytes({"tickers": ["TST", "TSTB"]}, newline=True)
        submissions_path.write_bytes(submissions_body)
        submissions_path.with_suffix(".json.meta.json").write_bytes(canonical_json_bytes({
            "sha256": hashlib.sha256(submissions_body).hexdigest(),
            "retrieved_at_utc": "2026-11-19T12:00:00Z",
        }, newline=True))
    facts_path = cache / "companyfacts" / "TST" / "CIK0000000001.json"
    facts_path.parent.mkdir(parents=True)
    facts = {
        "facts": {"us-gaap": {"Revenue": {"units": {"USD": [
            {"accn": "0001-26-000010", "form": "10-Q", "end": "2026-03-31",
             "fy": 2026, "fp": "Q1"},
            {"accn": "0001-26-000020", "form": "10-Q", "end": "2026-06-30",
             "fy": 2026, "fp": "Q2"},
        ]}}}},
    }
    facts_path.write_bytes(canonical_json_bytes(facts, newline=True))
    submission_dir = cache / "complete_submissions" / "TST"
    submission_dir.mkdir(parents=True)
    previous_submission = submission_dir / "0001-26-000011.txt"
    current_submission = submission_dir / "0001-26-000021.txt"
    previous_submission.write_bytes(
        b"<DOCUMENT><TYPE>EX-99.1\n<FILENAME>prev.htm\n<TEXT><p>previous quarter revenue text</p></TEXT></DOCUMENT>"
    )
    current_submission.write_bytes(
        b"<DOCUMENT><TYPE>EX-99.1\n<FILENAME>curr.htm\n<TEXT><p>current quarter different revenue text</p></TEXT></DOCUMENT>"
    )
    filings = [
        {"accessionNumber": "0001-26-000010", "form": "10-Q",
         "reportDate": "2026-03-31", "acceptanceDateTime": "2026-05-01T12:00:00Z"},
        {"accessionNumber": "0001-26-000011", "form": "8-K", "items": "2.02,9.01",
         "acceptanceDateTime": "2026-06-01T20:30:00Z",
         "complete_submission_relative_path": "complete_submissions/TST/0001-26-000011.txt"},
        {"accessionNumber": "0001-26-000020", "form": "10-Q",
         "reportDate": "2026-06-30", "acceptanceDateTime": "2026-08-01T12:00:00Z"},
        {"accessionNumber": "0001-26-000021", "form": "8-K", "items": "2.02,9.01",
         "acceptanceDateTime": "2026-10-01T20:30:00Z",
         "complete_submission_relative_path": "complete_submissions/TST/0001-26-000021.txt"},
    ]
    index = {
        "schema_name": "trinity.h2-v2-1-sec-corpus-index", "schema_version": "1",
        "issuers": [
            {"ticker": ticker, "cik": "0000000001",
             "companyfacts_relative_path": "companyfacts/TST/CIK0000000001.json",
             "filings": filings}
            for ticker in ("TST", "TSTB")
        ],
    }
    index_path = sec_root / "sec_corpus_index.json"
    index_path.write_bytes(canonical_json_bytes(index, newline=True))
    sec_files = file_inventory(sec_root, exclude_names=frozenset({"sec_acquisition_manifest.json"}))
    sec_manifest = {
        "schema_name": "trinity.h2-v2-1-sec-acquisition", "schema_version": "1",
        "index_sha256": hashlib.sha256(index_path.read_bytes()).hexdigest(),
        "files": sec_files,
        "ensemble_sha256": hashlib.sha256(canonical_json_bytes(sec_files)).hexdigest(),
    }
    (sec_root / "sec_acquisition_manifest.json").write_bytes(
        canonical_json_bytes(sec_manifest, newline=True)
    )

    price_root = tmp_path / "prices"
    price_file = price_root / "bounded_jsonl" / "TST.jsonl"
    price_file.parent.mkdir(parents=True)
    sessions = phase1.xnys_calendar().sessions_window("2026-10-01", -61)
    with price_file.open("wb") as handle:
        for ordinal, session in enumerate(sessions, start=1):
            handle.write(_price_line(session.date().isoformat(), 100.0 + ordinal))
    price_files = file_inventory(price_root, exclude_names=frozenset({"price_ensemble_manifest.json"}))
    price_manifest = {
        "schema_name": "trinity.h2-v2-1-price-ensemble", "schema_version": "1",
        "spec_id": phase1.SPEC_ID, "provider": "TWELVE_DATA",
        "snapshot_id": "SYNTHETIC", "issuer_snapshot_sha256": "a" * 64,
        "target_session": "2026-11-17", "acquisition_started_at_utc": "synthetic",
        "acquisition_completed_at_utc": "synthetic", "endpoint": "synthetic",
        "parameters": {}, "adjustment_policy": {}, "spy_normalized_sha256": "b" * 64,
        "files": price_files,
        "ensemble_sha256": hashlib.sha256(canonical_json_bytes(price_files)).hexdigest(),
    }
    (price_root / "price_ensemble_manifest.json").write_bytes(
        canonical_json_bytes(price_manifest, newline=True)
    )

    output = tmp_path / "output"
    result = build_phase1_inputs(
        repo_root=Path(phase1.__file__).resolve().parents[1], sec_root=sec_root,
        price_root=price_root, text_root=tmp_path / "texts", output_root=output,
        supported_by_ticker={"TST": "0000000001", "TSTB": "0000000001"},
    )
    assert result["dev_candidate_count"] == 1
    with (output / "h2_v2_1_inputs_dev.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["row_status"] == "INCLUDED"
    assert rows[0]["pairing_quality"] == "EXACT"
    assert rows[0]["sigma_status"] == "COMPLETE"
    with (output / "h2_v2_1_exclusions.csv").open(encoding="utf-8", newline="") as handle:
        exclusions = list(csv.DictReader(handle))
    duplicate_rows = [
        row for row in exclusions if row["reason"] == "DUPLICATE_SHARE_CLASS"
    ]
    assert len(duplicate_rows) == 2
    assert {row["ticker"] for row in duplicate_rows} == {"TSTB"}
    assert any(row["sample"] == "OUTSIDE" for row in duplicate_rows)
    assert any(row["sample"] == "DEV" for row in duplicate_rows)
    resolution = json.loads(
        (output / "h2_v2_1_share_class_resolution.json").read_text(encoding="utf-8")
    )
    assert resolution["cases"][0]["selected_ticker"] == "TST"
    assert resolution["cases"][0]["event_counts"] == {
        "DEV": 1, "HISTORICAL_PAIRING": 1, "OUTSIDE_WINDOW": 0,
        "PROSP_BATCH1": 0, "UNASSIGNED": 0,
    }
    assert result["duplicate_share_class_row_count"] == 2
    assert {
        "absolute_return_pct_20", "raw_return_pct_20", "excess_return_pct_20",
        "outcome_status_20", "pred_0", "pred_1",
    }.isdisjoint(rows[0])

