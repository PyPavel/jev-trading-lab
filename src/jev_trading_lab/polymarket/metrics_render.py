"""Strict deterministic Prometheus 0.0.4 exposition rendering and validation."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from jev_trading_lab.polymarket.metrics_schema import (
    METRIC_FAMILIES,
    MetricFamily,
    MetricType,
    metric_family,
    validate_labels,
)

_MAX_SERIES = 800
_MAX_HISTOGRAM_OBSERVATIONS = 800
_MAX_DECIMAL_DIGITS = 128
_MAX_DECIMAL_TEXT_LENGTH = _MAX_DECIMAL_DIGITS + 3
_MAX_LABEL_TEXT_LENGTH = 256
_MAX_EXPOSITION_LINE_BYTES = 512
_MAX_EXPOSITION_LINES = 2 * len(METRIC_FAMILIES) + _MAX_SERIES
_MAX_EXPOSITION_BYTES = 256 * 1024
_VALUE = re.compile(r"(?:0|-?[1-9][0-9]*(?:\.[0-9]*[1-9])?|-?0\.[0-9]*[1-9])\Z")
_SAMPLE = re.compile(
    r"(?P<name>[a-z_:][a-z0-9_:]*)(?:\{(?P<labels>.*)\})? (?P<value>\S+)\Z"
)
_LABEL_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*\Z")


@dataclass(frozen=True, slots=True)
class Sample:
    family: str
    labels: tuple[tuple[str, str], ...]
    value: int | Decimal


@dataclass(frozen=True, slots=True)
class Histogram:
    family: str
    labels: tuple[tuple[str, str], ...]
    observations: tuple[Decimal, ...]


class ExpositionError(ValueError):
    """The supported strict Prometheus exposition contract was violated."""


def _exact_decimal_sum(values: list[Decimal]) -> Decimal:
    """Add finite decimals by integer coefficients without using decimal context."""
    if not values:
        return Decimal(0)
    parts: list[tuple[int, tuple[int, ...], int]] = []
    for value in values:
        sign, digits, exponent = value.as_tuple()
        assert isinstance(exponent, int)
        parts.append((sign, digits, exponent))
    common_exponent = min(exponent for _, _, exponent in parts)
    total = 0
    for sign, digits, exponent in parts:
        coefficient = int("".join(map(str, digits))) if digits else 0
        total += (-coefficient if sign else coefficient) * 10 ** (
            exponent - common_exponent
        )
    sign = int(total < 0)
    digits = tuple(map(int, str(abs(total))))
    return Decimal((sign, digits, common_exponent))


def canonical_decimal(value: int | Decimal) -> str:
    """Return an exact finite plain decimal, normalizing trailing and negative zero."""
    if type(value) not in (int, Decimal):
        raise ExpositionError("metric values must be int or Decimal")
    decimal = Decimal(value)
    if not decimal.is_finite():
        raise ExpositionError("metric values must be finite")
    _, digits, exponent = decimal.as_tuple()
    assert isinstance(exponent, int)
    if (
        len(digits) > _MAX_DECIMAL_DIGITS
        or abs(exponent) > _MAX_DECIMAL_DIGITS
        or abs(decimal.adjusted()) > _MAX_DECIMAL_DIGITS
    ):
        raise ExpositionError("metric value exceeds decimal size limits")
    if decimal.is_zero():
        return "0"
    rendered = format(decimal, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _escape_help(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n")


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _family(name: str) -> MetricFamily:
    try:
        return metric_family(name)
    except (TypeError, ValueError) as error:
        raise ExpositionError("metric family is invalid") from error


def _labels(
    family: MetricFamily, supplied: tuple[tuple[str, str], ...]
) -> tuple[tuple[str, str], ...]:
    if type(supplied) is not tuple or len(supplied) != len(family.labels):
        raise ExpositionError("labels have invalid structure")
    if any(
        type(pair) is not tuple
        or len(pair) != 2
        or type(pair[0]) is not str
        or type(pair[1]) is not str
        for pair in supplied
    ):
        raise ExpositionError("labels have invalid structure")
    if len({name for name, _ in supplied}) != len(supplied):
        raise ExpositionError("labels have invalid structure")
    try:
        return validate_labels(family, dict(supplied))
    except (TypeError, ValueError) as error:
        raise ExpositionError("labels are invalid for metric family") from error


def _format_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    body = ",".join(f'{name}="{_escape_label(value)}"' for name, value in labels)
    return "{" + body + "}"


def _declarations() -> list[str]:
    lines: list[str] = []
    for family in METRIC_FAMILIES:
        lines.append(f"# HELP {family.name} {_escape_help(family.help)}")
        lines.append(f"# TYPE {family.name} {family.type.value}")
    return lines


def _check_series_budget(prospective_count: int) -> None:
    if prospective_count > _MAX_SERIES:
        raise ExpositionError(f"exposition exceeds {_MAX_SERIES} unique series")


def render_exposition(
    samples: Iterable[Sample], histograms: Iterable[Histogram]
) -> bytes:
    """Render, independently validate, and return one deterministic exposition."""
    series: list[tuple[str, tuple[tuple[str, str], ...], str]] = []
    keys: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    histogram_groups: set[tuple[str, tuple[tuple[str, str], ...]]] = set()

    for sample in samples:
        if type(sample) is not Sample:
            raise ExpositionError("samples must contain exact Sample values")
        if type(sample.family) is not str:
            raise ExpositionError("sample family must be str")
        family = _family(sample.family)
        if family.type is MetricType.HISTOGRAM:
            raise ExpositionError(f"histogram requires observations: {family.name}")
        labels = _labels(family, sample.labels)
        key = (family.name, labels)
        if key in keys:
            raise ExpositionError(
                f"duplicate series: {family.name}{_format_labels(labels)}"
            )
        _check_series_budget(len(series) + 1)
        keys.add(key)
        value = canonical_decimal(sample.value)
        if family.type is MetricType.COUNTER and Decimal(value) < 0:
            raise ExpositionError(f"counter must be nonnegative: {family.name}")
        series.append((family.name, labels, value))

    for histogram in histograms:
        if type(histogram) is not Histogram:
            raise ExpositionError("histograms must contain exact Histogram values")
        if type(histogram.family) is not str:
            raise ExpositionError("histogram family must be str")
        family = _family(histogram.family)
        if family.type is not MetricType.HISTOGRAM:
            raise ExpositionError(f"family is not a histogram: {family.name}")
        labels = _labels(family, histogram.labels)
        group = (family.name, labels)
        if group in histogram_groups:
            raise ExpositionError(
                f"duplicate histogram: {family.name}{_format_labels(labels)}"
            )
        histogram_series_count = len(family.buckets) + 3
        _check_series_budget(len(series) + histogram_series_count)
        histogram_groups.add(group)
        if type(histogram.observations) is not tuple:
            raise ExpositionError("histogram requires a bounded observation tuple")
        if len(histogram.observations) > _MAX_HISTOGRAM_OBSERVATIONS:
            raise ExpositionError("histogram observation tuple exceeds 800 values")
        observations: list[Decimal] = []
        for observation in histogram.observations:
            if type(observation) is not Decimal or not observation.is_finite():
                raise ExpositionError(
                    "histogram observations must be finite Decimal values"
                )
            if observation < 0:
                raise ExpositionError("histogram observations must be nonnegative")
            canonical_decimal(observation)
            observations.append(Decimal(observation))
        for bucket in (*family.buckets, None):
            count = (
                len(observations)
                if bucket is None
                else sum(observation <= bucket for observation in observations)
            )
            le = "+Inf" if bucket is None else canonical_decimal(bucket)
            bucket_labels = tuple(sorted((*labels, ("le", le))))
            series.append((family.name + "_bucket", bucket_labels, str(count)))
        series.append((family.name + "_count", labels, str(len(observations))))
        series.append(
            (
                family.name + "_sum",
                labels,
                canonical_decimal(_exact_decimal_sum(observations)),
            )
        )

    series.sort(key=lambda item: (item[0], item[1]))
    lines = _declarations()
    lines.extend(
        name + _format_labels(labels) + " " + value for name, labels, value in series
    )
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    validate_exposition(payload)
    return payload


def _decode_label_value(text: str, start: int) -> tuple[str, int]:
    value: list[str] = []
    index = start
    while index < len(text):
        char = text[index]
        if char == '"':
            return "".join(value), index + 1
        if char == "\\":
            index += 1
            if index == len(text) or text[index] not in {"\\", '"', "n"}:
                raise ExpositionError("malformed label escape")
            value.append("\n" if text[index] == "n" else text[index])
        elif char in {"\n", "\r"}:
            raise ExpositionError("literal newline in label value")
        else:
            value.append(char)
        index += 1
    raise ExpositionError("unterminated label value")


def _parse_labels(text: str | None) -> tuple[tuple[str, str], ...]:
    if text is None:
        return ()
    if len(text) > _MAX_LABEL_TEXT_LENGTH:
        raise ExpositionError("sample labels exceed size limit")
    if not text:
        raise ExpositionError("empty label braces are not canonical")
    pairs: list[tuple[str, str]] = []
    index = 0
    while index < len(text):
        equals = text.find("=", index)
        if equals < 0:
            raise ExpositionError("malformed label assignment")
        name = text[index:equals]
        if not _LABEL_NAME.fullmatch(name) or equals + 1 >= len(text):
            raise ExpositionError("malformed label name")
        if text[equals + 1] != '"':
            raise ExpositionError("label value must be quoted")
        value, index = _decode_label_value(text, equals + 2)
        pairs.append((name, value))
        if index == len(text):
            break
        if text[index] != ",":
            raise ExpositionError("malformed label separator")
        index += 1
        if index == len(text):
            raise ExpositionError("trailing label separator")
    result = tuple(pairs)
    if len({name for name, _ in result}) != len(result):
        raise ExpositionError("duplicate label name")
    if result != tuple(sorted(result)):
        raise ExpositionError("labels must be sorted")
    if _format_labels(result)[1:-1] != text:
        raise ExpositionError("labels are not canonically escaped")
    return result


def _parse_value(text: str) -> Decimal:
    if len(text) > _MAX_DECIMAL_TEXT_LENGTH:
        raise ExpositionError("metric value exceeds size limit")
    if not _VALUE.fullmatch(text):
        raise ExpositionError("metric value is noncanonical or nonfinite")
    try:
        value = Decimal(text)
    except InvalidOperation as error:
        raise ExpositionError("metric value is invalid") from error
    if not value.is_finite() or canonical_decimal(value) != text:
        raise ExpositionError("metric value is noncanonical or nonfinite")
    return value


def _sample_family(name: str) -> tuple[MetricFamily, str]:
    for suffix, component in (
        ("_bucket", "bucket"),
        ("_count", "count"),
        ("_sum", "sum"),
    ):
        if name.endswith(suffix):
            base_name = name.removesuffix(suffix)
            try:
                family = metric_family(base_name)
            except ValueError:
                continue
            if family.type is MetricType.HISTOGRAM:
                return family, component
    family = _family(name)
    if family.type is MetricType.HISTOGRAM:
        raise ExpositionError(f"bare histogram sample is not supported: {name}")
    return family, "sample"


def _validate_series_labels(
    family: MetricFamily,
    component: str,
    labels: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    label_map = dict(labels)
    if component == "bucket":
        if "le" not in label_map:
            raise ExpositionError(f"histogram bucket missing le: {family.name}")
        label_map.pop("le")
    elif "le" in label_map:
        raise ExpositionError(f"unexpected le label: {family.name}")
    canonical = _labels(family, tuple(label_map.items()))
    extra = (("le", dict(labels)["le"]),) if component == "bucket" else ()
    expected = tuple(sorted((*canonical, *extra)))
    if labels != expected:
        raise ExpositionError(f"wrong labels for {family.name}")
    return canonical


def _validate_histograms(
    groups: Mapping[tuple[str, tuple[tuple[str, str], ...]], dict[str, object]],
) -> None:
    for (family_name, _), parts in groups.items():
        family = metric_family(family_name)
        expected_bounds = tuple(
            canonical_decimal(value) for value in family.buckets
        ) + ("+Inf",)
        buckets = parts.get("bucket")
        count = parts.get("count")
        total = parts.get("sum")
        if not isinstance(buckets, dict) or set(buckets) != set(expected_bounds):
            raise ExpositionError(f"wrong histogram buckets: {family_name}")
        if not isinstance(count, Decimal) or not isinstance(total, Decimal):
            raise ExpositionError(f"incomplete histogram: {family_name}")
        ordered_counts = [buckets[bound] for bound in expected_bounds]
        if any(
            not isinstance(value, Decimal) or value != value.to_integral_value()
            for value in ordered_counts
        ):
            raise ExpositionError(
                f"histogram bucket counts must be integers: {family_name}"
            )
        if any(value < 0 for value in ordered_counts) or any(
            current > following
            for current, following in zip(ordered_counts, ordered_counts[1:])
        ):
            raise ExpositionError(
                f"histogram buckets must be cumulative: {family_name}"
            )
        if (
            count != count.to_integral_value()
            or count < 0
            or ordered_counts[-1] != count
        ):
            raise ExpositionError(f"histogram count mismatch: {family_name}")
        if total < 0:
            raise ExpositionError(f"histogram sum must be nonnegative: {family_name}")
        if count == 0 and total != 0:
            raise ExpositionError(f"empty histogram sum must be zero: {family_name}")


def validate_exposition(payload: bytes) -> int:
    """Strictly parse the supported exposition subset and return unique series count."""
    if type(payload) is not bytes:
        raise ExpositionError("exposition must be bytes")
    if len(payload) > _MAX_EXPOSITION_BYTES:
        raise ExpositionError("exposition exceeds size limit")
    if not payload.endswith(b"\n") or payload.endswith(b"\n\n") or b"\r" in payload:
        raise ExpositionError("exposition must have exactly one final LF")
    if payload.count(b"\n") > _MAX_EXPOSITION_LINES:
        raise ExpositionError("exposition exceeds line limit")
    byte_lines = payload[:-1].split(b"\n")
    if any(len(line) > _MAX_EXPOSITION_LINE_BYTES for line in byte_lines):
        raise ExpositionError("exposition line exceeds size limit")
    try:
        lines = [line.decode("utf-8") for line in byte_lines]
    except UnicodeDecodeError as error:
        raise ExpositionError("exposition must be valid UTF-8") from error
    if any(not line for line in lines):
        raise ExpositionError("blank exposition lines are forbidden")

    declarations = _declarations()
    if lines[: len(declarations)] != declarations:
        raise ExpositionError("declarations must be complete, unique, and lexical")
    sample_lines = lines[len(declarations) :]
    parsed: list[
        tuple[str, tuple[tuple[str, str], ...], Decimal, str, MetricFamily]
    ] = []
    keys: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    groups: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, object]] = {}

    for line in sample_lines:
        match = _SAMPLE.fullmatch(line)
        if match is None:
            raise ExpositionError("sample line is malformed")
        name = match.group("name")
        labels = _parse_labels(match.group("labels"))
        value = _parse_value(match.group("value"))
        family, component = _sample_family(name)
        if component == "sample" and family.type is MetricType.COUNTER and value < 0:
            raise ExpositionError(f"counter must be nonnegative: {family.name}")
        base_labels = _validate_series_labels(family, component, labels)
        key = (name, labels)
        if key in keys:
            raise ExpositionError(f"duplicate series: {name}{_format_labels(labels)}")
        keys.add(key)
        parsed.append((name, labels, value, component, family))
        if component != "sample":
            group = groups.setdefault((family.name, base_labels), {})
            if component == "bucket":
                buckets = group.setdefault("bucket", {})
                assert isinstance(buckets, dict)
                le = dict(labels)["le"]
                if le in buckets:
                    raise ExpositionError(f"duplicate histogram bucket: {family.name}")
                buckets[le] = value
            elif component in group:
                raise ExpositionError(f"duplicate histogram component: {family.name}")
            else:
                group[component] = value

    order = [(name, labels) for name, labels, _, _, _ in parsed]
    if order != sorted(order):
        raise ExpositionError("sample series must be sorted by metric name and labels")
    if len(keys) > _MAX_SERIES:
        raise ExpositionError(f"exposition exceeds {_MAX_SERIES} unique series")
    _validate_histograms(groups)
    return len(keys)
