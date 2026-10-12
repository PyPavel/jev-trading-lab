"""Derive useful paper-trading metrics from verified observation state."""

from __future__ import annotations

import base64
import os
import secrets
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

from jev_trading_lab.polymarket.ai import JEV_MODEL_ID, OPENAI_MODEL_ID, _usage_terms
from jev_trading_lab.polymarket.call_journal import ProviderCallJournal
from jev_trading_lab.polymarket.metrics_render import (
    Histogram,
    Sample,
    render_exposition,
)
from jev_trading_lab.polymarket.models import ArmResult, BookSnapshot, ObservationResult
from jev_trading_lab.polymarket.operations import OperationKind, load_operation_cut
from jev_trading_lab.polymarket.store import load_observation

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
            ("capital_gross_exposure_usd", _usd(exposure)),
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
        if arms and all(arm.economic_pnl_microdollars is not None for arm in arms):
            economic = sum(arm.economic_pnl_microdollars or 0 for arm in arms)
            samples.extend(
                (
                    Sample(
                        "jev_polymarket_economic_pnl_usd",
                        _labels(arm=arm_name),
                        _usd(economic),
                    ),
                    Sample(
                        "jev_polymarket_capital_equity_usd",
                        _labels(arm=arm_name),
                        _usd(initial + economic),
                    ),
                )
            )
    return samples


def _provider_metrics(output: Path) -> tuple[list[Sample], list[Histogram]]:
    samples: list[Sample] = []
    durations: dict[str, list[Decimal]] = {provider: [] for provider in _MODELS}
    calls: Counter[tuple[str, str]] = Counter()
    tokens: Counter[tuple[str, str]] = Counter()
    costs: Counter[str] = Counter()
    unknown_usage: Counter[str] = Counter()
    unknown_cost: Counter[str] = Counter()
    pending: Counter[str] = Counter()
    recoveries: Counter[tuple[str, str]] = Counter()
    for event in load_operation_cut(output).events:
        if (
            event.kind is OperationKind.RECOVERY
            and event.provider in _MODELS
            and event.reason in {"reused_result", "unknown_in_flight"}
        ):
            recoveries[(event.provider, event.reason)] += 1
    for record in ProviderCallJournal(output).snapshot():
        provider = cast(str, record.get("provider"))
        if provider not in _MODELS:
            outcome = record.get("outcome")
            if not isinstance(outcome, dict):
                continue
            provider = cast(str, outcome.get("provider", ""))
        if provider not in _MODELS:
            # Results omit provider because it is bound by their verified intent.
            call_id = cast(str, record["call_id"])
            state = ProviderCallJournal(output).state(call_id)
            if state is None:
                continue
            # Recover provider from the verified request model for legacy records.
            request = base64.b64decode(state["outcome"]["request_b64"], validate=True)
            decoded = __import__("json").loads(request)
            provider = "openai" if decoded.get("model") == OPENAI_MODEL_ID else "jev"
        model = _MODELS[provider]
        base = _labels(provider=provider, model_snapshot=model)
        if record["kind"] == "provider_call_intent":
            pending[provider] += 1
            unknown_cost[provider] += 1
            continue
        outcome = cast(dict[str, object], record["outcome"])
        kind = outcome["type"]
        status = "success"
        if kind == "AdapterCostOverflow":
            status = "cost_overflow"
            unknown_cost[provider] += 1
        elif kind == "AdapterFailure":
            status = (
                "unknown_in_flight"
                if outcome.get("code") == "UNKNOWN_IN_FLIGHT"
                else "failure"
            )
        calls[(provider, status)] += 1
        cost = outcome.get("billed_cost_microdollars")
        if type(cost) is int:
            costs[provider] += cost
        elif kind != "AdapterCostOverflow":
            unknown_cost[provider] += 1
        response_b64 = outcome.get("response_b64")
        usage = None
        if type(response_b64) is str:
            usage = _usage_terms(
                base64.b64decode(response_b64, validate=True),
                cast(Literal["openai", "jev"], provider),
            )
        if usage is None:
            unknown_usage[provider] += 1
        else:
            tokens[(provider, "input")] += usage[0]
            tokens[(provider, "cached")] += usage[1] or 0
            tokens[(provider, "output")] += usage[2]
        elapsed = record.get("elapsed_nanoseconds")
        if type(elapsed) is int:
            durations[provider].append(Decimal(elapsed) / Decimal(1_000_000_000))
    histograms: list[Histogram] = []
    active = (
        set(provider for provider, _status in calls)
        | set(pending)
        | set(provider for provider, _status in recoveries)
    )
    for provider in sorted(active):
        model = _MODELS[provider]
        base = _labels(provider=provider, model_snapshot=model)
        samples.append(Sample("jev_polymarket_llm_model_snapshot_active", base, 1))
        for (item_provider, status), count in calls.items():
            if item_provider == provider:
                samples.append(
                    Sample(
                        "jev_polymarket_llm_calls_total",
                        _labels(provider=provider, model_snapshot=model, status=status),
                        count,
                    )
                )
        for status in ("reused_result", "unknown_in_flight"):
            if recoveries[(provider, status)]:
                samples.append(
                    Sample(
                        "jev_polymarket_llm_restart_recoveries_total",
                        _labels(provider=provider, model_snapshot=model, status=status),
                        recoveries[(provider, status)],
                    )
                )
        if pending[provider]:
            samples.append(
                Sample(
                    "jev_polymarket_llm_restart_recoveries_total",
                    _labels(provider=provider, model_snapshot=model, status="pending"),
                    pending[provider],
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
        if unknown_cost[provider]:
            samples.append(
                Sample(
                    "jev_polymarket_llm_unknown_cost_calls_total",
                    base,
                    unknown_cost[provider],
                )
            )
        if durations[provider]:
            histograms.append(
                Histogram(
                    "jev_polymarket_llm_call_duration_seconds",
                    base,
                    tuple(durations[provider]),
                )
            )
    return samples, histograms


def _operation_metrics(
    output: Path,
) -> tuple[list[Sample], list[Histogram], datetime | None]:
    samples: list[Sample] = []
    histograms: list[Histogram] = []
    cut = load_operation_cut(output)
    market_calls: Counter[tuple[str, str]] = Counter()
    market_failures: Counter[tuple[str, str]] = Counter()
    market_durations: dict[str, list[Decimal]] = {"gamma": [], "clob": []}
    run_counts: Counter[str] = Counter()
    terminals = [
        event for event in cut.events if event.kind is OperationKind.RUN_TERMINAL
    ]
    for event in cut.events:
        if event.kind is OperationKind.STAGE_TERMINAL and event.provider in {
            "gamma",
            "clob",
        }:
            status = "timeout" if event.status == "timeout" else event.status
            market_calls[(event.provider, status)] += 1
            if status != "success" and event.reason is not None:
                market_failures[(event.provider, event.reason)] += 1
            if event.elapsed_nanoseconds is not None:
                market_durations[event.provider].append(
                    Decimal(event.elapsed_nanoseconds) / Decimal(1_000_000_000)
                )
        elif event.kind is OperationKind.RUN_TERMINAL:
            run_counts[event.status] += 1
    for (provider, status), count in market_calls.items():
        samples.append(
            Sample(
                "jev_polymarket_market_data_calls_total",
                _labels(provider=provider, status=status),
                count,
            )
        )
    for (provider, reason), count in market_failures.items():
        samples.append(
            Sample(
                "jev_polymarket_market_data_failures_total",
                _labels(provider=provider, reason=reason),
                count,
            )
        )
    for provider, values in market_durations.items():
        if values:
            histograms.append(
                Histogram(
                    "jev_polymarket_market_data_call_duration_seconds",
                    _labels(provider=provider),
                    tuple(values),
                )
            )
    for status, count in run_counts.items():
        samples.append(
            Sample("jev_polymarket_pipeline_runs_total", _labels(status=status), count)
        )
    latest = max(
        terminals, key=lambda event: (event.occurred_at, event.event_id), default=None
    )
    if latest is not None and latest.elapsed_nanoseconds is not None:
        samples.append(
            Sample(
                "jev_polymarket_process_last_run_runtime_seconds",
                _labels(status=latest.status),
                Decimal(latest.elapsed_nanoseconds) / Decimal(1_000_000_000),
            )
        )
    max_timestamp = max((event.occurred_at for event in cut.events), default=None)
    return samples, histograms, max_timestamp


def render_paper_metrics(output_dir: str | os.PathLike[str]) -> Path:
    """Validate results, derive bounded metrics, and atomically publish a textfile."""

    output = Path(output_dir).absolute()
    results = _read_results(output)
    operation_samples, operation_histograms, operation_max = _operation_metrics(output)
    source_times = [result.observed_at for result in results]
    if operation_max is not None:
        source_times.append(operation_max)
    generated_at = max(source_times) if source_times else datetime.now(UTC)
    samples = _arm_samples(output, results)
    if results:
        samples.append(
            Sample(
                "jev_polymarket_metrics_source_unknown_total",
                _labels(reason="capital"),
                1,
            )
        )
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
            )
        )
    samples.extend(operation_samples)
    provider_samples, provider_histograms = _provider_metrics(output)
    samples.extend(provider_samples)
    payload = render_exposition(samples, (*provider_histograms, *operation_histograms))
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
