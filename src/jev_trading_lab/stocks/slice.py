"""Minimal paper-only stock observation slice."""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, NotRequired, TypedDict, cast

from jev_trading_lab.core.ids import canonical_json_bytes

DEFAULT_SYMBOLS: Final = ("AAPL", "MSFT", "NVDA")
_ALLOWED_SYMBOLS = frozenset(DEFAULT_SYMBOLS)
_STARTING_CASH = 10_000_000_000  # $10,000 in microdollars
_POSITION_BUDGET = _STARTING_CASH // 10
_OPENAI_MODEL = "gpt-5-2025-08-07"
_JEV_MODEL = "jev-1.13.0"


class Quote(TypedDict):
    symbol: str
    timestamp: str
    price_micros: int
    previous_close_micros: int
    request_status: str
    request_latency_ms: int
    age_seconds: int


class Decision(TypedDict):
    provider: Literal["openai", "jev", "deterministic"]
    model: str
    called: bool
    action: Literal["BUY", "HOLD"]
    symbol: str | None
    confidence_micros: int
    status: str
    latency_ms: int
    input_tokens: int | None
    output_tokens: int | None
    cost_microdollars: int | None
    source_provenance: NotRequired[str]


_OPENAI_DECISION_SCHEMA: Final = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["BUY", "HOLD"]},
        "symbol": {"type": ["string", "null"], "enum": [*DEFAULT_SYMBOLS, None]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["action", "symbol", "confidence"],
    "additionalProperties": False,
}


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("stock fixture must be a JSON object")
    return value


def _symbols(value: str) -> tuple[str, ...]:
    symbols = tuple(part.strip().upper() for part in value.split(",") if part.strip())
    if (
        not symbols
        or len(symbols) > len(DEFAULT_SYMBOLS)
        or len(set(symbols)) != len(symbols)
    ):
        raise ValueError("symbols must be a unique non-empty subset of AAPL,MSFT,NVDA")
    if not set(symbols) <= _ALLOWED_SYMBOLS:
        raise ValueError("symbols must be a subset of AAPL,MSFT,NVDA")
    return symbols


def _validate_quotes(
    value: object, symbols: tuple[str, ...] | None = None
) -> list[Quote]:
    if not isinstance(value, list) or not value:
        raise ValueError("quotes must be a non-empty list")
    quotes: list[Quote] = []
    expected = set(symbols) if symbols is not None else None
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("quote must be an object")
        symbol = raw.get("symbol")
        timestamp = raw.get("timestamp")
        price = raw.get("price_micros")
        previous = raw.get("previous_close_micros")
        latency = raw.get("request_latency_ms")
        age = raw.get("age_seconds")
        status = raw.get("request_status")
        if (
            type(symbol) is not str
            or symbol not in _ALLOWED_SYMBOLS
            or type(timestamp) is not str
            or type(price) is not int
            or price <= 0
            or type(previous) is not int
            or previous <= 0
            or type(latency) is not int
            or latency < 0
            or type(age) is not int
            or age < 0
            or status != "success"
        ):
            raise ValueError("quote fields are invalid")
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        quotes.append(cast(Quote, raw))
    if len({quote["symbol"] for quote in quotes}) != len(quotes):
        raise ValueError("quote symbols must be unique")
    if expected is not None and {quote["symbol"] for quote in quotes} != expected:
        raise ValueError("Yahoo snapshot did not contain every requested symbol")
    return sorted(quotes, key=lambda quote: quote["symbol"])


def _fetch_quote(symbol: str) -> Quote:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
    request = urllib.request.Request(url, headers={"User-Agent": "jev-trading-lab/0.1"})
    started = time.perf_counter_ns()
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
        if (
            response.status != 200
            or response.headers.get_content_type() != "application/json"
        ):
            raise ValueError("Yahoo chart request failed")
        body = response.read(1_000_001)
    latency = (time.perf_counter_ns() - started) // 1_000_000
    if len(body) > 1_000_000:
        raise ValueError("Yahoo chart response exceeded byte limit")
    try:
        chart = json.loads(body)["chart"]["result"][0]
        timestamps = chart["timestamp"]
        closes = chart["indicators"]["quote"][0]["close"]
        points = [
            (stamp, close)
            for stamp, close in zip(timestamps, closes)
            if close is not None
        ]
        (previous_stamp, previous_close), (stamp, close) = points[-2:]
        del previous_stamp
        observed = datetime.fromtimestamp(stamp, UTC)
        now = datetime.now(UTC)
        price = round(float(close) * 1_000_000)
        previous = round(float(previous_close) * 1_000_000)
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise ValueError("Yahoo chart response was invalid") from error
    return {
        "symbol": symbol,
        "timestamp": observed.isoformat().replace("+00:00", "Z"),
        "price_micros": price,
        "previous_close_micros": previous,
        "request_status": "success",
        "request_latency_ms": latency,
        "age_seconds": max(0, int((now - observed).total_seconds())),
    }


def _provider_cost(
    arm: Literal["openai", "jev"], usage: dict[str, object]
) -> int | None:
    try:
        input_tokens = usage["input_tokens"]
        output_tokens = usage["output_tokens"]
        if type(input_tokens) is not int or type(output_tokens) is not int:
            return None
        if arm == "openai":
            details = usage.get("input_tokens_details")
            cached = details.get("cached_tokens", 0) if isinstance(details, dict) else 0
            if type(cached) is not int or not 0 <= cached <= input_tokens:
                cached = 0
            openai_rates = (
                int(os.environ["OPENAI_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS"]),
                int(
                    os.environ[
                        "OPENAI_CACHED_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS"
                    ]
                ),
                int(os.environ["OPENAI_OUTPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS"]),
            )
            numerator = (
                (input_tokens - cached) * openai_rates[0]
                + cached * openai_rates[1]
                + output_tokens * openai_rates[2]
            )
            minimum_rate = min(openai_rates)
        else:
            jev_rates = (
                int(os.environ["TYPESAFE_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS"]),
                int(os.environ["TYPESAFE_OUTPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS"]),
            )
            numerator = input_tokens * jev_rates[0] + output_tokens * jev_rates[1]
            minimum_rate = min(jev_rates)
        if minimum_rate < 0:
            return None
        return (numerator + 999_999) // 1_000_000
    except (KeyError, TypeError, ValueError):
        return None


def _provider_decision(
    arm: Literal["openai", "jev"], snapshot: bytes, quotes: list[Quote]
) -> Decision:
    key_name = "OPENAI_API_KEY" if arm == "openai" else "TYPESAFE_API_KEY"
    key = os.environ.get(key_name)
    if not key:
        return {
            "provider": arm,
            "model": _OPENAI_MODEL if arm == "openai" else _JEV_MODEL,
            "called": False,
            "action": "HOLD",
            "symbol": None,
            "confidence_micros": 0,
            "status": "configuration_failure",
            "latency_ms": 0,
            "input_tokens": None,
            "output_tokens": None,
            "cost_microdollars": None,
        }
    prompt = (
        "Paper research only. Using this frozen stock snapshot, return JSON with "
        "action BUY or HOLD, symbol AAPL/MSFT/NVDA or null, and confidence 0..1. "
        + snapshot.decode()
    )
    if arm == "openai":
        url = "https://api.openai.com/v1/responses"
        body = canonical_json_bytes(
            {
                "model": _OPENAI_MODEL,
                "input": prompt,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "stock_decision",
                        "strict": True,
                        "schema": _OPENAI_DECISION_SCHEMA,
                    }
                },
            }
        )
    else:
        url = "https://api.typesafe.ai/v1/systemone"
        body = canonical_json_bytes(
            {
                "model": _JEV_MODEL,
                "state": {"snapshot": json.loads(snapshot)},
                "questions": {
                    "action": {
                        "type": "choice",
                        "criteria": {"BUY": "Buy", "HOLD": "Hold"},
                    },
                    "symbol": {
                        "type": "choice",
                        "criteria": {
                            **{q["symbol"]: q["symbol"] for q in quotes},
                            "NONE": "None",
                        },
                    },
                    "confidence": {"type": "score", "criteria": ["No", "Yes"]},
                },
            }
        )
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter_ns()
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            raw = response.read(1_000_001)
            status_code = response.status
        latency = (time.perf_counter_ns() - started) // 1_000_000
        if status_code != 200 or len(raw) > 1_000_000:
            raise ValueError("provider response invalid")
        envelope = json.loads(raw)
        usage = envelope.get("usage", {})
        if arm == "openai":
            messages = [
                item
                for item in envelope["output"]
                if isinstance(item, dict)
                and item.get("type") == "message"
                and item.get("status") == "completed"
            ]
            if len(messages) != 1:
                raise ValueError("provider response must contain one completed message")
            output_texts = [
                content["text"]
                for content in messages[0].get("content", [])
                if isinstance(content, dict) and content.get("type") == "output_text"
            ]
            if len(output_texts) != 1 or not isinstance(output_texts[0], str):
                raise ValueError(
                    "provider response must contain one completed output text"
                )
            answer = json.loads(output_texts[0])
        else:
            answers = envelope["answers"]
            answer = {
                "action": answers["action"]["choice"],
                "symbol": answers["symbol"]["choice"],
                "confidence": answers["confidence"]["score"],
            }
            if answer["symbol"] == "NONE":
                answer["symbol"] = None
        action = answer["action"]
        symbol = answer.get("symbol")
        confidence = answer["confidence"]
        if (
            action not in {"BUY", "HOLD"}
            or symbol not in {None, *(quote["symbol"] for quote in quotes)}
            or type(confidence) not in {int, float}
            or not 0 <= confidence <= 1
            or (action == "BUY") != (symbol is not None)
        ):
            raise ValueError("provider decision invalid")
        return {
            "provider": arm,
            "model": _OPENAI_MODEL if arm == "openai" else _JEV_MODEL,
            "called": True,
            "action": action,
            "symbol": symbol,
            "confidence_micros": round(confidence * 1_000_000),
            "status": "success",
            "latency_ms": latency,
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cost_microdollars": _provider_cost(arm, usage),
        }
    except (
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        urllib.error.URLError,
    ):
        return {
            "provider": arm,
            "model": _OPENAI_MODEL if arm == "openai" else _JEV_MODEL,
            "called": True,
            "action": "HOLD",
            "symbol": None,
            "confidence_micros": 0,
            "status": "provider_failure",
            "latency_ms": (time.perf_counter_ns() - started) // 1_000_000,
            "input_tokens": None,
            "output_tokens": None,
            "cost_microdollars": None,
        }


def _validate_fixture_decision(
    arm: Literal["openai", "jev"], value: object, symbols: set[str]
) -> Decision:
    if not isinstance(value, dict):
        raise ValueError("stock fixture decision is invalid")
    expected_model = _OPENAI_MODEL if arm == "openai" else _JEV_MODEL
    action = value.get("action")
    symbol = value.get("symbol")
    called = value.get("called")
    status = value.get("status")
    numeric_fields = (
        value.get("latency_ms"),
        value.get("input_tokens"),
        value.get("output_tokens"),
        value.get("cost_microdollars"),
    )
    valid_numbers = all(
        item is None or (type(item) is int and item >= 0) for item in numeric_fields
    )
    valid_action = (
        action in {"BUY", "HOLD"}
        and symbol in {None, *symbols}
        and (action == "BUY") == (symbol is not None)
    )
    confidence = value.get("confidence_micros")
    synthetic = (
        called is False
        and status == "synthetic_replay"
        and value.get("latency_ms") == 0
        and all(
            value.get(field) is None
            for field in ("input_tokens", "output_tokens", "cost_microdollars")
        )
    )
    captured = (
        called is True
        and status in {"success", "provider_failure"}
        and isinstance(value.get("source_provenance"), str)
        and bool(value["source_provenance"])
    )
    if (
        value.get("provider") != arm
        or value.get("model") != expected_model
        or not valid_action
        or type(confidence) is not int
        or not 0 <= confidence <= 1_000_000
        or not valid_numbers
        or not (synthetic or captured)
    ):
        raise ValueError("stock fixture decision is invalid")
    return cast(Decision, value)


def _control(quotes: list[Quote]) -> Decision:
    winner = max(
        quotes, key=lambda quote: quote["price_micros"] / quote["previous_close_micros"]
    )
    gain = winner["price_micros"] - winner["previous_close_micros"]
    action: Literal["BUY", "HOLD"] = "BUY" if gain > 0 else "HOLD"
    return {
        "provider": "deterministic",
        "model": "momentum-v1",
        "called": False,
        "action": action,
        "symbol": winner["symbol"] if action == "BUY" else None,
        "confidence_micros": min(
            1_000_000, max(0, gain * 1_000_000 // winner["previous_close_micros"])
        ),
        "status": "not_applicable",
        "latency_ms": 0,
        "input_tokens": None,
        "output_tokens": None,
        "cost_microdollars": 0,
    }


def _row(
    observation_id: str,
    observed_at: str,
    snapshot_sha256: str,
    quotes: list[Quote],
    arm: str,
    decision: Decision,
    cycle_duration_ms: int,
    source_mode: str = "live_public",
) -> dict[str, object]:
    quote = next(
        (item for item in quotes if item["symbol"] == decision["symbol"]), None
    )
    quantity = 0
    fill_price = None
    slippage = 0
    if decision["action"] == "BUY" and quote is not None:
        fill_price = (quote["price_micros"] * 10_005 + 9_999) // 10_000
        quantity = _POSITION_BUDGET // fill_price
        slippage = (fill_price - quote["price_micros"]) * quantity
    notional = (fill_price or 0) * quantity
    cash = _STARTING_CASH - notional
    liquidation = quote["price_micros"] * 9_995 // 10_000 if quote is not None else 0
    position_value = liquidation * quantity
    unrealized = position_value - notional
    trading_equity = cash + position_value
    trading_pnl = trading_equity - _STARTING_CASH
    cost_unknown = decision["called"] and decision["cost_microdollars"] is None
    provider_cost = decision["cost_microdollars"] or 0
    economic_equity = None if cost_unknown else trading_equity - provider_cost
    economic_pnl = None if economic_equity is None else economic_equity - _STARTING_CASH
    terminal = (
        "completed"
        if decision["status"] in {"success", "not_applicable", "synthetic_replay"}
        and decision["action"] == "BUY"
        else "abstained"
        if decision["status"] in {"success", "not_applicable", "synthetic_replay"}
        else "failed"
    )
    return {
        "schema_version": 1,
        "source_mode": source_mode,
        "observation_id": observation_id,
        "observed_at": observed_at,
        "snapshot_sha256": snapshot_sha256,
        "arm": arm,
        "market_data": {
            "provider": "yahoo_chart_v8",
            "request_count": len(quotes),
            "quotes": quotes,
        },
        "provider": decision,
        "decision": {
            "action": decision["action"],
            "symbol": decision["symbol"],
            "confidence_micros": decision["confidence_micros"],
        },
        "fill": {
            "price_microdollars": fill_price,
            "fees_microdollars": 0,
            "slippage_microdollars": slippage,
            "quantity": quantity,
            "round_trip_bps": 10,
        },
        "initial_cash_microdollars": _STARTING_CASH,
        "cash_microdollars": cash,
        "position_quantity": quantity,
        "position_value_microdollars": position_value,
        "equity_microdollars": trading_equity,
        "trading_equity_microdollars": trading_equity,
        "realized_pnl_microdollars": 0,
        "unrealized_pnl_microdollars": unrealized,
        "trading_pnl_microdollars": trading_pnl,
        "provider_cost_microdollars": decision["cost_microdollars"],
        "economic_cost_unknown": cost_unknown,
        "economic_equity_microdollars": economic_equity,
        "economic_pnl_microdollars": economic_pnl,
        "failure": None if terminal != "failed" else decision["status"],
        "abstention": terminal == "abstained",
        "terminal_status": terminal,
        "process": {"cycle_duration_ms": cycle_duration_ms},
    }


def _append_rows(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = b"".join(canonical_json_bytes(row) + b"\n" for row in rows)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        view = memoryview(payload)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _metric(name: str, labels: Mapping[str, object], value: object) -> str:
    label_text = ",".join(f'{key}="{item}"' for key, item in labels.items())
    return f"{name}{{{label_text}}} {value}"


def _write_metrics(path: Path, rows: list[dict[str, object]]) -> None:
    lines = ["# Stock paper metrics derived from terminal JSONL rows."]
    market_data = cast(dict[str, object], rows[0]["market_data"])
    market_provider = str(market_data["provider"])
    quotes = cast(list[Quote], market_data["quotes"])
    market_status = str(market_data.get("request_status", "success"))
    market_labels = {"provider": market_provider, "status": market_status}
    lines.append(
        _metric(
            "jev_stocks_market_data_requests_total",
            market_labels,
            market_data["request_count"],
        )
    )
    labels = {"provider": market_provider}
    if quotes:
        lines.extend(
            [
                _metric(
                    "jev_stocks_market_data_request_latency_milliseconds",
                    labels,
                    max(quote["request_latency_ms"] for quote in quotes),
                ),
                _metric(
                    "jev_stocks_market_data_age_seconds",
                    labels,
                    max(quote["age_seconds"] for quote in quotes),
                ),
            ]
        )
    elif "request_latency_ms" in market_data:
        lines.append(
            _metric(
                "jev_stocks_market_data_request_latency_milliseconds",
                labels,
                market_data["request_latency_ms"],
            )
        )

    cycle_status = (
        "failure" if any(row["arm"] == "cycle" for row in rows) else "success"
    )
    lines.append(_metric("jev_stocks_cycle_status", {"status": cycle_status}, 1))
    for row in sorted(rows, key=lambda item: str(item["arm"])):
        arm = str(row["arm"])
        labels = {"arm": arm}
        terminal_status = str(row["terminal_status"])
        process = cast(dict[str, int], row["process"])
        lines.extend(
            [
                _metric(
                    "jev_stocks_failures_total",
                    labels,
                    int(terminal_status == "failed"),
                ),
                _metric(
                    "jev_stocks_abstentions_total",
                    labels,
                    int(terminal_status == "abstained"),
                ),
                _metric(
                    "jev_stocks_cycle_duration_milliseconds",
                    labels,
                    process["cycle_duration_ms"],
                ),
            ]
        )
        if arm == "cycle":
            continue
        provider = cast(Decision, row["provider"])
        provider_labels = {
            "provider": provider["provider"],
            "model": provider["model"],
        }
        status_labels = {**provider_labels, "status": provider["status"]}
        decision = cast(dict[str, object], row["decision"])
        fill = cast(dict[str, object], row["fill"])
        lines.extend(
            [
                _metric(
                    "jev_stocks_provider_calls_total",
                    provider_labels,
                    int(provider["called"]),
                ),
                _metric("jev_stocks_provider_status", status_labels, 1),
                _metric(
                    "jev_stocks_provider_input_tokens_total",
                    provider_labels,
                    provider["input_tokens"] or 0,
                ),
                _metric(
                    "jev_stocks_provider_output_tokens_total",
                    provider_labels,
                    provider["output_tokens"] or 0,
                ),
                _metric(
                    "jev_stocks_provider_latency_milliseconds",
                    provider_labels,
                    provider["latency_ms"],
                ),
                _metric(
                    "jev_stocks_provider_cost_microdollars_total",
                    provider_labels,
                    provider["cost_microdollars"] or 0,
                ),
                _metric(
                    "jev_stocks_provider_unknown_cost",
                    provider_labels,
                    int(
                        provider["called"]
                        and provider["cost_microdollars"] is None
                    ),
                ),
                _metric(
                    "jev_stocks_decisions_total",
                    {"arm": arm, "action": decision["action"]},
                    1,
                ),
                _metric(
                    "jev_stocks_fill_price_microdollars",
                    labels,
                    fill["price_microdollars"] or 0,
                ),
                _metric(
                    "jev_stocks_fill_fees_microdollars",
                    labels,
                    fill["fees_microdollars"],
                ),
                _metric(
                    "jev_stocks_fill_slippage_microdollars",
                    labels,
                    fill["slippage_microdollars"],
                ),
                _metric("jev_stocks_fill_quantity", labels, fill["quantity"]),
                _metric(
                    "jev_stocks_cash_microdollars", labels, row["cash_microdollars"]
                ),
                _metric(
                    "jev_stocks_position_quantity", labels, row["position_quantity"]
                ),
                _metric(
                    "jev_stocks_position_value_microdollars",
                    labels,
                    row["position_value_microdollars"],
                ),
                _metric(
                    "jev_stocks_trading_equity_microdollars",
                    labels,
                    row["trading_equity_microdollars"],
                ),
                _metric(
                    "jev_stocks_trading_pnl_microdollars",
                    labels,
                    row["trading_pnl_microdollars"],
                ),
            ]
        )
        if row["economic_equity_microdollars"] is not None:
            lines.extend(
                [
                    _metric(
                        "jev_stocks_economic_equity_microdollars",
                        labels,
                        row["economic_equity_microdollars"],
                    ),
                    _metric(
                        "jev_stocks_economic_pnl_microdollars",
                        labels,
                        row["economic_pnl_microdollars"],
                    ),
                ]
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _run(
    observation_id: str,
    observed_at: str,
    quotes: list[Quote],
    decisions: dict[str, Decision],
    output_dir: Path,
    started_ns: int,
    source_mode: str,
) -> dict[str, object]:
    snapshot = canonical_json_bytes({"observed_at": observed_at, "quotes": quotes})
    digest = hashlib.sha256(snapshot).hexdigest()
    rows = [
        _row(
            observation_id,
            observed_at,
            digest,
            quotes,
            arm,
            decision,
            (time.perf_counter_ns() - started_ns) // 1_000_000,
            source_mode,
        )
        for arm, decision in decisions.items()
    ]
    result_path = output_dir / "results.jsonl"
    metrics_path = output_dir / "stocks.prom"
    _append_rows(result_path, rows)
    _write_metrics(metrics_path, rows)
    return {
        "arms": {str(row["arm"]): row["terminal_status"] for row in rows},
        "metrics_path": str(metrics_path),
        "observation_id": observation_id,
        "result_path": str(result_path),
        "terminal_rows": len(rows),
    }


def replay_fixture(fixture: Path, output_dir: Path) -> dict[str, object]:
    """Run one deterministic, offline three-arm observation."""
    started = time.perf_counter_ns()
    value = _load_json(fixture)
    observation_id = value.get("observation_id")
    observed_at = value.get("observed_at")
    decisions = value.get("fixture_decisions")
    if (
        type(observation_id) is not str
        or type(observed_at) is not str
        or not isinstance(decisions, dict)
    ):
        raise ValueError("stock fixture metadata is invalid")
    quotes = _validate_quotes(value.get("quotes"))
    typed: dict[str, Decision] = {}
    fixture_mode = value.get("source_mode")
    if fixture_mode not in {"captured_replay", "synthetic_replay"}:
        raise ValueError("stock fixture source mode is invalid")
    symbols = {quote["symbol"] for quote in quotes}
    for arm in ("openai", "jev"):
        decision = _validate_fixture_decision(arm, decisions.get(arm), symbols)
        if fixture_mode == "synthetic_replay" and (
            decision["called"] or decision["status"] != "synthetic_replay"
        ):
            raise ValueError("stock fixture decision is invalid")
        if fixture_mode == "captured_replay" and (
            not decision["called"] or "source_provenance" not in decision
        ):
            raise ValueError("stock fixture decision is invalid")
        typed[arm] = decision
    typed["deterministic"] = _control(quotes)
    return _run(
        observation_id,
        observed_at,
        quotes,
        typed,
        output_dir,
        started,
        fixture_mode,
    )


def _market_data_failure_row(
    observation_id: str,
    observed_at: str,
    request_count: int,
    request_latency_ms: int,
    cycle_duration_ms: int,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "source_mode": "live_public",
        "observation_id": observation_id,
        "observed_at": observed_at,
        "snapshot_sha256": None,
        "arm": "cycle",
        "market_data": {
            "provider": "yahoo_chart_v8",
            "request_count": request_count,
            "request_status": "failure",
            "request_latency_ms": request_latency_ms,
            "quotes": [],
        },
        "failure": "market_data_failure",
        "abstention": False,
        "terminal_status": "failed",
        "process": {"cycle_duration_ms": cycle_duration_ms},
    }


def observe_public(symbols: str, output_dir: Path) -> dict[str, object]:
    """Fetch one Yahoo snapshot and make paper-only decisions; never place orders."""
    started = time.perf_counter_ns()
    selected = _symbols(symbols)
    quotes: list[Quote] = []
    request_count = 0
    try:
        for symbol in selected:
            request_count += 1
            quotes.append(_fetch_quote(symbol))
        quotes = _validate_quotes(quotes, selected)
    except (OSError, ValueError, urllib.error.URLError):
        observed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        observation_id = (
            "stocks-failed-"
            + hashlib.sha256(
                canonical_json_bytes(
                    {"observed_at": observed_at, "symbols": list(selected)}
                )
            ).hexdigest()[:20]
        )
        rows = [
            _market_data_failure_row(
                observation_id,
                observed_at,
                request_count,
                (time.perf_counter_ns() - started) // 1_000_000,
                (time.perf_counter_ns() - started) // 1_000_000,
            )
        ]
        result_path = output_dir / "results.jsonl"
        metrics_path = output_dir / "stocks.prom"
        _append_rows(result_path, rows)
        _write_metrics(metrics_path, rows)
        return {
            "arms": {"cycle": "failed"},
            "metrics_path": str(metrics_path),
            "observation_id": observation_id,
            "result_path": str(result_path),
            "terminal_rows": 1,
        }
    observed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    snapshot = canonical_json_bytes({"observed_at": observed_at, "quotes": quotes})
    observation_id = "stocks-" + hashlib.sha256(snapshot).hexdigest()[:20]
    decisions: dict[str, Decision] = {
        "openai": _provider_decision("openai", snapshot, quotes),
        "jev": _provider_decision("jev", snapshot, quotes),
        "deterministic": _control(quotes),
    }
    return _run(
        observation_id,
        observed_at,
        quotes,
        decisions,
        output_dir,
        started,
        "live_public",
    )
