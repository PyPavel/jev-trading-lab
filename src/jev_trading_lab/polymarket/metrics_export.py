"""Derive useful paper-trading metrics from verified observation state."""

from __future__ import annotations

import os
import secrets
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

from jev_trading_lab.polymarket.ai import JEV_MODEL_ID, OPENAI_MODEL_ID, _usage_terms
from jev_trading_lab.polymarket.metrics_render import (
    Histogram,
    Sample,
    render_exposition,
)
from jev_trading_lab.polymarket.models import ArmResult, BookSnapshot, ObservationResult
from jev_trading_lab.polymarket.store import (
    FailureArtifact,
    SuccessArtifact,
    load_observation,
)

_USD = Decimal(1_000_000)
_MODELS = {"openai": OPENAI_MODEL_ID, "jev": JEV_MODEL_ID}
_DISPOSITIONS = {
    "FILLED": "filled",
    "PARTIAL_FILL": "partial_fill",
    "ABSTAIN": "abstain",
    "NO_EDGE": "no_edge",
    "INELIGIBLE": "ineligible",
    "MODEL_FAILURE": "model_failure",
    "BOOK_FAILURE": "book_failure",
    "COST_OVERFLOW": "cost_overflow",
    "UNKNOWN_IN_FLIGHT": "unknown_in_flight",
}


def _labels(**values: str) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(values.items()))


def _usd(value: int) -> Decimal:
    return Decimal(value) / _USD


def _seconds(value: datetime) -> Decimal:
    return Decimal(str(value.timestamp()))


def _read_results(output: Path) -> list[ObservationResult]:
    path = output / "results.jsonl"
    if not path.exists():
        return []
    return [
        ObservationResult.model_validate_json(line)
        for line in path.read_bytes().splitlines()
    ]


def _terminal(result: ObservationResult) -> tuple[str, str]:
    dispositions = {result.ai.disposition, result.deterministic.disposition}
    if dispositions & {"MODEL_FAILURE", "UNKNOWN_IN_FLIGHT"}:
        return "failed", "model_failure"
    if "BOOK_FAILURE" in dispositions:
        return "failed", "book_failure"
    if "INELIGIBLE" in dispositions:
        return "rejected", "other"
    if dispositions & {"FILLED", "PARTIAL_FILL"}:
        return "filled", "none"
    return "no_fill", "abstain" if "ABSTAIN" in dispositions else "no_edge"


def _arm_samples(output: Path, results: list[ObservationResult]) -> list[Sample]:
    samples: list[Sample] = []
    for arm_name, attribute in (("ai", "ai"), ("deterministic", "deterministic")):
        arms: list[ArmResult] = [getattr(result, attribute) for result in results]
        dispositions = Counter(_DISPOSITIONS[arm.disposition] for arm in arms)
        for status, count in dispositions.items():
            samples.append(
                Sample(
                    "jev_polymarket_arm_dispositions_total",
                    _labels(arm=arm_name, status=status),
                    count,
                )
            )
        fills = Counter(
            "partial" if arm.disposition == "PARTIAL_FILL" else "full"
            for arm in arms
            if arm.quantity
        )
        for status, count in fills.items():
            samples.append(
                Sample(
                    "jev_polymarket_fills_total",
                    _labels(arm=arm_name, status=status),
                    count,
                )
            )
        quantity = sum(arm.quantity for arm in arms)
        notional = sum(arm.entry_notional_microdollars or 0 for arm in arms)
        fees = sum(arm.fees_microdollars for arm in arms)
        trading = sum(arm.trading_pnl_microdollars for arm in arms)
        realized = sum(
            arm.trading_pnl_microdollars
            for arm in arms
            if arm.payout_micros is not None
        )
        unrealized = sum(
            arm.trading_pnl_microdollars
            for arm in arms
            if arm.quantity and arm.payout_micros is None
        )
        initial = arms[-1].initial_cash_microdollars if arms else 0
        resolved_value = sum(
            arm.valuation_notional_microdollars or 0
            for arm in arms
            if arm.payout_micros is not None
        )
        cash = initial - notional - fees + resolved_value
        exposure = sum(
            arm.valuation_notional_microdollars or 0
            for arm in arms
            if arm.payout_micros is None
        )
        slippage = 0
        for result, arm in zip(results, arms, strict=True):
            if not arm.quantity or arm.entry_notional_microdollars is None:
                continue
            book = load_observation(output, result.observation_id).typed_artifacts.get(
                "execution_book"
            )
            if not isinstance(book, BookSnapshot):
                continue
            benchmark = (
                book.asks[0].price_micros
                if arm.entry_side == "YES"
                else 1_000_000 - book.bids[0].price_micros
            )
            slippage += arm.entry_notional_microdollars - arm.quantity * benchmark
        for family, value in (
            ("fill_quantity_contracts_total", quantity),
            ("fill_entry_notional_usd_total", _usd(notional)),
            ("fees_usd_total", _usd(fees)),
            ("slippage_usd_total", _usd(slippage)),
            ("realized_pnl_usd", _usd(realized)),
            ("unrealized_pnl_usd", _usd(unrealized)),
            ("trading_pnl_usd", _usd(trading)),
            ("capital_initial_usd", _usd(initial)),
            ("capital_cash_usd", _usd(cash)),
            ("capital_gross_exposure_usd", _usd(exposure)),
            ("capital_equity_usd", _usd(cash + exposure)),
        ):
            samples.append(
                Sample("jev_polymarket_" + family, _labels(arm=arm_name), value)
            )
        if quantity:
            samples.extend(
                (
                    Sample(
                        "jev_polymarket_fill_mean_quantity_contracts",
                        _labels(arm=arm_name),
                        Decimal(quantity) / sum(arm.quantity > 0 for arm in arms),
                    ),
                    Sample(
                        "jev_polymarket_fill_entry_vwap_probability",
                        _labels(arm=arm_name),
                        Decimal(notional) / Decimal(quantity) / _USD,
                    ),
                )
            )
        if all(arm.economic_pnl_microdollars is not None for arm in arms):
            samples.append(
                Sample(
                    "jev_polymarket_economic_pnl_usd",
                    _labels(arm=arm_name),
                    _usd(sum(arm.economic_pnl_microdollars or 0 for arm in arms)),
                )
            )
    return samples


def _provider_metrics(
    output: Path, results: list[ObservationResult]
) -> tuple[list[Sample], list[Histogram]]:
    samples: list[Sample] = []
    durations: dict[str, list[Decimal]] = {provider: [] for provider in _MODELS}
    calls: Counter[tuple[str, str]] = Counter()
    tokens: Counter[tuple[str, str]] = Counter()
    costs: Counter[str] = Counter()
    unknown_usage: Counter[str] = Counter()
    for result in results:
        replay = load_observation(output, result.observation_id)
        for provider in _MODELS:
            request = replay.envelopes.get(f"{provider}_request")
            success = replay.typed_artifacts.get(f"{provider}_success_metadata")
            failure = replay.typed_artifacts.get(f"{provider}_failure")
            overflow = replay.typed_artifacts.get(f"{provider}_cost_overflow")
            if request is None and failure is None:
                continue
            status = "success"
            finished_at: datetime | None = None
            if isinstance(success, SuccessArtifact):
                costs[provider] += success.billed_cost_microdollars
                finished_at = success.retrieved_at
            elif isinstance(failure, FailureArtifact):
                status = (
                    "unknown_in_flight"
                    if failure.code == "UNKNOWN_IN_FLIGHT"
                    else "failure"
                )
                if failure.billed_cost_microdollars is not None:
                    costs[provider] += failure.billed_cost_microdollars
                finished_at = failure.retrieved_at
            elif overflow is not None:
                status = "cost_overflow"
                finished_at = getattr(overflow, "retrieved_at", None)
            calls[(provider, status)] += 1
            response = replay.artifacts.get(f"{provider}_response_success")
            usage = (
                _usage_terms(response, cast(Literal["openai", "jev"], provider))
                if response is not None
                else None
            )
            if usage is None:
                unknown_usage[provider] += 1
            else:
                tokens[(provider, "input")] += usage[0]
                tokens[(provider, "cached")] += usage[1] or 0
                tokens[(provider, "output")] += usage[2]
            if request is not None and finished_at is not None:
                elapsed = (finished_at - request.retrieved_at).total_seconds()
                if elapsed >= 0:
                    durations[provider].append(Decimal(str(elapsed)))
    histograms: list[Histogram] = []
    for provider, model in _MODELS.items():
        if not any(item_provider == provider for item_provider, _status in calls):
            continue
        base = _labels(provider=provider, model_snapshot=model)
        samples.append(Sample("jev_polymarket_llm_model_snapshot_active", base, 1))
        for (item_provider, status), count in calls.items():
            if item_provider == provider:
                samples.append(
                    Sample(
                        "jev_polymarket_llm_calls_total",
                        _labels(
                            provider=provider,
                            model_snapshot=model,
                            status=status,
                        ),
                        count,
                    )
                )
        samples.extend(
            (
                Sample(
                    "jev_polymarket_llm_input_tokens_total",
                    base,
                    tokens[(provider, "input")],
                ),
                Sample(
                    "jev_polymarket_llm_output_tokens_total",
                    base,
                    tokens[(provider, "output")],
                ),
                Sample(
                    "jev_polymarket_llm_billed_cost_usd_total",
                    base,
                    _usd(costs[provider]),
                ),
            )
        )
        if provider == "openai":
            samples.append(
                Sample(
                    "jev_polymarket_llm_cached_input_tokens_total",
                    base,
                    tokens[(provider, "cached")],
                )
            )
        if unknown_usage[provider]:
            samples.append(
                Sample(
                    "jev_polymarket_llm_token_usage_unknown_total",
                    base,
                    unknown_usage[provider],
                )
            )
        histograms.append(
            Histogram(
                "jev_polymarket_llm_call_duration_seconds",
                base,
                tuple(durations[provider]),
            )
        )
    return samples, histograms


def _market_metrics(
    output: Path, results: list[ObservationResult], generated_at: datetime
) -> tuple[list[Sample], list[Histogram]]:
    samples: list[Sample] = []
    durations: list[Decimal] = []
    book_ages: dict[str, list[Decimal]] = {"precheck_book": [], "execution_book": []}
    calls: Counter[tuple[str, str]] = Counter()
    latest_poll: datetime | None = None
    for result in results:
        replay = load_observation(output, result.observation_id)
        calls[("gamma", "success")] += 1
        latest_poll = max(latest_poll or result.cutoff_at, result.cutoff_at)
        if (
            "precheck_book_raw" in replay.artifacts
            or "precheck_book" in replay.artifacts
        ):
            calls[("clob", "success")] += 1
        if "execution_book" in replay.artifacts:
            calls[("clob", "success")] += 1
        for stage in book_ages:
            book = replay.typed_artifacts.get(stage)
            if book is None:
                continue
            retrieved = getattr(book, "response_retrieved_at", None) or getattr(
                book, "retrieved_at"
            )
            age = (retrieved - getattr(book, "retrieved_at")).total_seconds()
            if age >= 0:
                book_ages[stage].append(Decimal(str(age)))
    for (provider, status), count in calls.items():
        samples.append(
            Sample(
                "jev_polymarket_market_data_calls_total",
                _labels(provider=provider, status=status),
                count,
            )
        )
    if latest_poll is not None:
        samples.extend(
            (
                Sample(
                    "jev_polymarket_market_data_last_poll_timestamp_seconds",
                    _labels(status="success"),
                    _seconds(latest_poll),
                ),
                Sample(
                    "jev_polymarket_market_data_poll_age_seconds",
                    _labels(status="success"),
                    Decimal(str((generated_at - latest_poll).total_seconds())),
                ),
            )
        )
    for stage, ages in book_ages.items():
        if ages:
            samples.append(
                Sample(
                    "jev_polymarket_market_data_latest_book_age_seconds",
                    _labels(stage=stage),
                    ages[-1],
                )
            )
    histograms = [
        Histogram(
            "jev_polymarket_market_data_call_duration_seconds",
            _labels(provider=provider),
            tuple(durations),
        )
        for provider in {provider for provider, _status in calls}
    ]
    return samples, histograms


def render_paper_metrics(output_dir: str | os.PathLike[str]) -> Path:
    """Validate results, derive bounded metrics, and atomically publish a textfile."""

    output = Path(output_dir).absolute()
    results = _read_results(output)
    generated_at = (
        max(result.observed_at for result in results) if results else datetime.now(UTC)
    )
    samples = _arm_samples(output, results)
    terminal = Counter(_terminal(result) for result in results)
    for (status, reason), count in terminal.items():
        samples.append(
            Sample(
                "jev_polymarket_pipeline_terminal_results_total",
                _labels(status=status, reason=reason),
                count,
            )
        )
    if results:
        samples.extend(
            (
                Sample(
                    "jev_polymarket_opportunities_total",
                    _labels(status="terminal"),
                    len(results),
                ),
                Sample(
                    "jev_polymarket_pipeline_runs_total",
                    _labels(status="success"),
                    len(results),
                ),
                Sample("jev_polymarket_metrics_schema_version", (), 1),
                Sample(
                    "jev_polymarket_metrics_snapshot_generation_timestamp_seconds",
                    (),
                    _seconds(generated_at),
                ),
                Sample(
                    "jev_polymarket_metrics_snapshot_source_max_timestamp_seconds",
                    (),
                    _seconds(max(result.observed_at for result in results)),
                ),
                Sample(
                    "jev_polymarket_process_last_run_runtime_seconds",
                    _labels(status="success"),
                    Decimal(
                        str(
                            (
                                max(result.observed_at for result in results)
                                - min(result.cutoff_at for result in results)
                            ).total_seconds()
                        )
                    ),
                ),
            )
        )
    provider_samples, provider_histograms = _provider_metrics(output, results)
    market_samples, market_histograms = _market_metrics(output, results, generated_at)
    samples.extend(provider_samples)
    samples.extend(market_samples)
    payload = render_exposition(samples, (*provider_histograms, *market_histograms))
    target = output / "polymarket.prom"
    temporary = output / f".polymarket.prom.{secrets.token_hex(16)}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("metrics write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        os.close(descriptor)
    os.replace(temporary, target)
    root_fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(root_fd)
    finally:
        os.close(root_fd)
    return target
