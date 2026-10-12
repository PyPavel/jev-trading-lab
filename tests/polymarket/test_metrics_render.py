from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal, localcontext
from itertools import product

import pytest

from jev_trading_lab.polymarket.metrics_render import (
    ExpositionError,
    Histogram,
    Sample,
    canonical_decimal,
    render_exposition,
    validate_exposition,
)
from jev_trading_lab.polymarket.metrics_schema import (
    METRIC_FAMILIES,
    MetricType,
    validate_labels,
)


def _sample_lines(payload: bytes) -> list[str]:
    return [
        line
        for line in payload.decode("utf-8").splitlines()
        if not line.startswith("#")
    ]


def _replace_once(payload: bytes, old: bytes, new: bytes) -> bytes:
    assert old in payload
    return payload.replace(old, new, 1)


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        (0, "0"),
        (7, "7"),
        (-7, "-7"),
        (Decimal("0.000001"), "0.000001"),
        (Decimal("1.2300"), "1.23"),
        (Decimal("1E+3"), "1000"),
        (Decimal("-0.000"), "0"),
        (Decimal("-12.3400"), "-12.34"),
    ),
)
def test_canonical_decimal_uses_finite_plain_exact_values(
    value: int | Decimal, expected: str
) -> None:
    assert canonical_decimal(value) == expected
    assert "E" not in expected.casefold()
    assert expected != "-0"


@pytest.mark.parametrize(
    "value",
    (
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        1.5,
        True,
    ),
)
def test_canonical_decimal_rejects_nonfinite_or_inexact_types(value: object) -> None:
    with pytest.raises(ExpositionError):
        canonical_decimal(value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    (
        Decimal("1e1000000"),
        Decimal("1e-1000000"),
        Decimal("1" * 129),
    ),
)
def test_canonical_decimal_rejects_output_amplification_before_formatting(
    value: Decimal,
) -> None:
    with pytest.raises(ExpositionError, match="size"):
        canonical_decimal(value)


def test_empty_exposition_declares_every_family_lexically() -> None:
    payload = render_exposition([], [])
    lines = payload.decode("utf-8").splitlines()

    assert payload.endswith(b"\n") and not payload.endswith(b"\n\n")
    assert "" not in lines
    assert len(lines) == 2 * len(METRIC_FAMILIES) == 210
    assert lines[::2] == [
        "# HELP " + family.name + " " + family.help.replace("\\", "\\\\")
        for family in METRIC_FAMILIES
    ]
    assert lines[1::2] == [
        f"# TYPE {family.name} {family.type.value}" for family in METRIC_FAMILIES
    ]
    assert validate_exposition(payload) == 0


def test_renderer_canonicalizes_labels_and_sorts_series() -> None:
    payload = render_exposition(
        [
            Sample(
                "jev_polymarket_fills_total",
                (("status", "full"), ("arm", "deterministic")),
                Decimal("2.500"),
            ),
            Sample("jev_polymarket_metrics_series", (), 2),
            Sample(
                "jev_polymarket_fills_total",
                (("arm", "ai"), ("status", "partial")),
                1,
            ),
        ],
        [],
    )

    assert _sample_lines(payload) == [
        'jev_polymarket_fills_total{arm="ai",status="partial"} 1',
        'jev_polymarket_fills_total{arm="deterministic",status="full"} 2.5',
        "jev_polymarket_metrics_series 2",
    ]
    assert validate_exposition(payload) == 3


@pytest.mark.parametrize(
    "sample",
    (
        Sample("not_registered", (), 1),
        Sample("jev_polymarket_metrics_series", (("extra", "x"),), 1),
        Sample("jev_polymarket_fills_total", (("arm", "ai"),), 1),
        Sample(
            "jev_polymarket_fills_total",
            (("arm", "ai"), ("arm", "ai"), ("status", "full")),
            1,
        ),
        Sample(
            "jev_polymarket_fills_total",
            (("arm", "unbounded"), ("status", "full")),
            1,
        ),
        Sample(
            "jev_polymarket_llm_calls_total",
            (
                ("provider", "openai"),
                ("model_snapshot", "jev-1.13.0"),
                ("status", "success"),
            ),
            1,
        ),
        Sample(
            "jev_polymarket_llm_call_duration_seconds",
            (("provider", "jev"), ("model_snapshot", "jev-1.13.0")),
            1,
        ),
    ),
)
def test_renderer_does_not_trust_sample_family_or_labels(sample: Sample) -> None:
    with pytest.raises(ExpositionError):
        render_exposition([sample], [])


@pytest.mark.parametrize(
    "histogram",
    (
        Histogram("not_registered", (), ()),
        Histogram("jev_polymarket_metrics_series", (), ()),
        Histogram(
            "jev_polymarket_market_data_call_duration_seconds",
            (("provider", "unknown"),),
            (),
        ),
        Histogram(
            "jev_polymarket_market_data_call_duration_seconds",
            (("provider", "gamma"), ("provider", "gamma")),
            (),
        ),
        Histogram(
            "jev_polymarket_market_data_call_duration_seconds",
            (("provider", "gamma"),),
            (Decimal("NaN"),),
        ),
    ),
)
def test_renderer_does_not_trust_histogram_family_labels_or_values(
    histogram: Histogram,
) -> None:
    with pytest.raises(ExpositionError):
        render_exposition([], [histogram])


def test_renderer_rejects_duplicate_series() -> None:
    sample = Sample("jev_polymarket_metrics_series", (), 1)
    histogram = Histogram(
        "jev_polymarket_market_data_call_duration_seconds",
        (("provider", "gamma"),),
        (),
    )

    with pytest.raises(ExpositionError, match="duplicate"):
        render_exposition([sample, sample], [])
    with pytest.raises(ExpositionError, match="duplicate"):
        render_exposition([], [histogram, histogram])


def test_sample_generator_has_bounded_consumption() -> None:
    consumed = 0
    sample = Sample("jev_polymarket_metrics_series", (), 1)

    def samples() -> Iterator[Sample]:
        nonlocal consumed
        while True:
            consumed += 1
            if consumed > 2:
                raise AssertionError("renderer consumed beyond duplicate rejection")
            yield sample

    with pytest.raises(ExpositionError, match="duplicate"):
        render_exposition(samples(), [])
    assert consumed == 2


def test_renderer_rejects_negative_counter_and_histogram_observation() -> None:
    with pytest.raises(ExpositionError, match="counter"):
        render_exposition(
            [
                Sample(
                    "jev_polymarket_fills_total",
                    (("arm", "ai"), ("status", "full")),
                    -1,
                )
            ],
            [],
        )
    with pytest.raises(ExpositionError, match="histogram"):
        render_exposition(
            [],
            [
                Histogram(
                    "jev_polymarket_market_data_call_duration_seconds",
                    (("provider", "gamma"),),
                    (Decimal("-0.1"),),
                )
            ],
        )


def test_histogram_has_fixed_cumulative_buckets_exact_sum_and_count() -> None:
    payload = render_exposition(
        [],
        [
            Histogram(
                "jev_polymarket_market_data_call_duration_seconds",
                (("provider", "gamma"),),
                (Decimal("0.05"), Decimal("0.10"), Decimal("2")),
            )
        ],
    )
    lines = _sample_lines(payload)

    prefix = "jev_polymarket_market_data_call_duration_seconds"
    assert lines == [
        prefix + '_bucket{le="+Inf",provider="gamma"} 3',
        prefix + '_bucket{le="0.05",provider="gamma"} 1',
        prefix + '_bucket{le="0.1",provider="gamma"} 2',
        prefix + '_bucket{le="0.25",provider="gamma"} 2',
        prefix + '_bucket{le="0.5",provider="gamma"} 2',
        prefix + '_bucket{le="1",provider="gamma"} 2',
        prefix + '_bucket{le="10",provider="gamma"} 3',
        prefix + '_bucket{le="2.5",provider="gamma"} 3',
        prefix + '_bucket{le="20",provider="gamma"} 3',
        prefix + '_bucket{le="30",provider="gamma"} 3',
        prefix + '_bucket{le="5",provider="gamma"} 3',
        prefix + '_count{provider="gamma"} 3',
        prefix + '_sum{provider="gamma"} 2.15',
    ]
    assert validate_exposition(payload) == 13


@pytest.mark.parametrize(
    ("precision", "rounding", "observations", "expected"),
    (
        (
            28,
            "ROUND_HALF_EVEN",
            (Decimal("1e27"), Decimal("0.1")),
            "1000000000000000000000000000.1",
        ),
        (2, "ROUND_DOWN", (Decimal("1.23"), Decimal("4.56")), "5.79"),
    ),
)
def test_histogram_sum_is_exact_under_arbitrary_decimal_context(
    precision: int,
    rounding: str,
    observations: tuple[Decimal, ...],
    expected: str,
) -> None:
    with localcontext() as context:
        context.prec = precision
        context.rounding = rounding
        payload = render_exposition(
            [],
            [
                Histogram(
                    "jev_polymarket_market_data_call_duration_seconds",
                    (("provider", "gamma"),),
                    observations,
                )
            ],
        )

    assert f'_sum{{provider="gamma"}} {expected}' in payload.decode("utf-8")


def _bounded_samples(limit: int) -> list[Sample]:
    samples: list[Sample] = []
    for family in METRIC_FAMILIES:
        if family.type is MetricType.HISTOGRAM:
            continue
        choices = (sorted(family.label_values[label]) for label in family.labels)
        for values in product(*choices):
            labels = dict(zip(family.labels, values, strict=True))
            try:
                canonical = validate_labels(family, labels)
            except ValueError:
                continue
            samples.append(Sample(family.name, canonical, 0))
            if len(samples) == limit:
                return samples
    raise AssertionError(f"registry has fewer than {limit} bounded sample series")


def test_series_budget_accepts_800_and_rejects_801() -> None:
    histogram = Histogram(
        "jev_polymarket_market_book_bid_near_depth_contracts_histogram",
        (("stage", "precheck_book"),),
        (),
    )
    payload = render_exposition(_bounded_samples(788), [histogram])

    assert validate_exposition(payload) == 800
    with pytest.raises(ExpositionError, match="800"):
        render_exposition(_bounded_samples(789), [histogram])


def test_histogram_structural_budget_is_checked_before_observations() -> None:
    def observations() -> Iterator[Decimal]:
        raise AssertionError("over-budget histogram consumed observations")
        yield Decimal(0)

    histogram = Histogram(
        "jev_polymarket_market_book_bid_near_depth_contracts_histogram",
        (("stage", "precheck_book"),),
        observations(),
    )

    with pytest.raises(ExpositionError, match="800"):
        render_exposition(_bounded_samples(789), [histogram])


def test_histogram_observation_generator_has_bounded_consumption() -> None:
    consumed = 0

    def observations() -> Iterator[Decimal]:
        nonlocal consumed
        while True:
            consumed += 1
            if consumed > 801:
                raise AssertionError("renderer consumed beyond rejection point")
            yield Decimal(0)

    histogram = Histogram(
        "jev_polymarket_market_data_call_duration_seconds",
        (("provider", "gamma"),),
        observations(),
    )

    with pytest.raises(ExpositionError, match="800"):
        render_exposition([], [histogram])
    assert consumed == 801


def test_render_validates_complete_result_before_returning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject(_: bytes) -> int:
        raise ExpositionError("independent validation failed")

    monkeypatch.setattr(
        "jev_trading_lab.polymarket.metrics_render.validate_exposition", reject
    )

    with pytest.raises(ExpositionError, match="independent validation failed"):
        render_exposition([], [])


def test_validator_rejects_malformed_framing_declarations_and_samples() -> None:
    empty = render_exposition([], [])
    one = render_exposition(
        [
            Sample(
                "jev_polymarket_fills_total",
                (("arm", "ai"), ("status", "full")),
                1,
            )
        ],
        [],
    )
    line = b'jev_polymarket_fills_total{arm="ai",status="full"} 1'
    cases = (
        empty.removesuffix(b"\n"),
        empty + b"\n",
        _replace_once(empty, b"# HELP ", b"# HULP "),
        _replace_once(empty, b" gauge\n", b" counter\n"),
        empty + b"undeclared_metric 1\n",
        one.replace(line, line + b" 123"),
        one.replace(line, line.replace(b'arm="ai"', b'arm="ai\\x"')),
        one.replace(line, line.replace(b" 1", b" NaN")),
        one.replace(line, line.replace(b'arm="ai"', b'extra="ai"')),
        one.replace(line + b"\n", line + b"\n" + line + b"\n"),
    )

    for payload in cases:
        with pytest.raises(ExpositionError):
            validate_exposition(payload)


def test_validator_rejects_noncanonical_values_and_series_order() -> None:
    payload = render_exposition(
        [
            Sample(
                "jev_polymarket_fills_total",
                (("arm", "ai"), ("status", "full")),
                1,
            ),
            Sample("jev_polymarket_metrics_series", (), 2),
        ],
        [],
    )
    first, second = _sample_lines(payload)
    swapped = payload.replace(
        (first + "\n" + second).encode(), (second + "\n" + first).encode()
    )

    with pytest.raises(ExpositionError):
        validate_exposition(payload.replace(b" 1\n", b" 1.0\n", 1))
    with pytest.raises(ExpositionError, match="sorted"):
        validate_exposition(swapped)


def test_validator_rejects_negative_counter() -> None:
    payload = render_exposition(
        [
            Sample(
                "jev_polymarket_fills_total",
                (("arm", "ai"), ("status", "full")),
                1,
            )
        ],
        [],
    )

    with pytest.raises(ExpositionError, match="counter"):
        validate_exposition(payload.replace(b" 1\n", b" -1\n", 1))


def test_validator_accepts_semantic_histogram_sum_without_observations() -> None:
    payload = render_exposition(
        [],
        [
            Histogram(
                "jev_polymarket_market_data_call_duration_seconds",
                (("provider", "gamma"),),
                (Decimal("0.1"), Decimal("2")),
            )
        ],
    )
    changed_sum = payload.replace(
        b'_sum{provider="gamma"} 2.1', b'_sum{provider="gamma"} 1.5'
    )

    assert validate_exposition(changed_sum) == 13


def test_validator_rejects_negative_nonnegative_histogram_sum() -> None:
    payload = render_exposition(
        [],
        [
            Histogram(
                "jev_polymarket_market_data_call_duration_seconds",
                (("provider", "gamma"),),
                (Decimal("0.1"), Decimal("2")),
            )
        ],
    )

    with pytest.raises(ExpositionError, match="histogram sum"):
        validate_exposition(
            payload.replace(b'_sum{provider="gamma"} 2.1', b'_sum{provider="gamma"} -1')
        )


@pytest.mark.parametrize(
    ("old", "new"),
    (
        (
            b'_bucket{le="0.05",provider="gamma"} 0',
            b'_bucket{le="0.05",provider="gamma"} -1',
        ),
        (
            b'_bucket{le="0.1",provider="gamma"} 1',
            b'_bucket{le="0.1",provider="gamma"} 0.5',
        ),
        (b'_count{provider="gamma"} 2', b'_count{provider="gamma"} -2'),
        (b'_count{provider="gamma"} 2', b'_count{provider="gamma"} 2.5'),
    ),
)
def test_validator_rejects_nonintegral_or_negative_histogram_counts(
    old: bytes, new: bytes
) -> None:
    payload = render_exposition(
        [],
        [
            Histogram(
                "jev_polymarket_market_data_call_duration_seconds",
                (("provider", "gamma"),),
                (Decimal("0.1"), Decimal("2")),
            )
        ],
    )

    with pytest.raises(ExpositionError):
        validate_exposition(payload.replace(old, new))


def test_validator_rejects_broken_histogram_invariants() -> None:
    payload = render_exposition(
        [],
        [
            Histogram(
                "jev_polymarket_market_data_call_duration_seconds",
                (("provider", "gamma"),),
                (Decimal("0.1"), Decimal("2")),
            )
        ],
    )
    bucket_prefix = b"jev_polymarket_market_data_call_duration_seconds_bucket"
    cases = (
        payload.replace(
            bucket_prefix + b'{le="0.05",provider="gamma"} 0\n',
            b"",
        ),
        payload.replace(b'le="0.05"', b'le="0.050"'),
        payload.replace(
            b'_bucket{le="+Inf",provider="gamma"} 2',
            b'_bucket{le="+Inf",provider="gamma"} 1',
        ),
        payload.replace(
            b'_bucket{le="0.25",provider="gamma"} 1',
            b'_bucket{le="0.25",provider="gamma"} 0',
        ),
        payload.replace(b'_count{provider="gamma"} 2', b'_count{provider="gamma"} 1'),
        payload.replace(b'_sum{provider="gamma"} 2.1', b'_sum{provider="gamma"} Inf'),
    )

    for malformed in cases:
        with pytest.raises(ExpositionError):
            validate_exposition(malformed)
