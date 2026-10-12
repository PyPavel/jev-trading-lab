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


def canonical_decimal(value: int | Decimal) -> str:
    """Return an exact finite plain decimal, normalizing trailing and negative zero."""
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ExpositionError("metric values must be int or Decimal")
    decimal = Decimal(value)
    if not decimal.is_finite():
        raise ExpositionError("metric values must be finite")
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
        raise ExpositionError(str(error)) from error


def _labels(
    family: MetricFamily, supplied: tuple[tuple[str, str], ...]
) -> tuple[tuple[str, str], ...]:
    try:
        pairs = tuple(supplied)
        if any(
            not isinstance(pair, tuple)
            or len(pair) != 2
            or not all(isinstance(item, str) for item in pair)
            for pair in pairs
        ):
            raise ExpositionError("labels must be string pairs")
        if len({name for name, _ in pairs}) != len(pairs):
            raise ExpositionError(f"duplicate labels for {family.name}")
        return validate_labels(family, dict(pairs))
    except (TypeError, ValueError) as error:
        if isinstance(error, ExpositionError):
            raise
        raise ExpositionError(str(error)) from error


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


def render_exposition(
    samples: Iterable[Sample], histograms: Iterable[Histogram]
) -> bytes:
    """Render, independently validate, and return one deterministic exposition."""
    series: list[tuple[str, tuple[tuple[str, str], ...], str]] = []
    keys: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    histogram_groups: set[tuple[str, tuple[tuple[str, str], ...]]] = set()

    for sample in samples:
        if not isinstance(sample, Sample):
            raise ExpositionError("samples must contain Sample values")
        family = _family(sample.family)
        if family.type is MetricType.HISTOGRAM:
            raise ExpositionError(f"histogram requires observations: {family.name}")
        labels = _labels(family, sample.labels)
        key = (family.name, labels)
        if key in keys:
            raise ExpositionError(
                f"duplicate series: {family.name}{_format_labels(labels)}"
            )
        keys.add(key)
        series.append((family.name, labels, canonical_decimal(sample.value)))

    for histogram in histograms:
        if not isinstance(histogram, Histogram):
            raise ExpositionError("histograms must contain Histogram values")
        family = _family(histogram.family)
        if family.type is not MetricType.HISTOGRAM:
            raise ExpositionError(f"family is not a histogram: {family.name}")
        labels = _labels(family, histogram.labels)
        group = (family.name, labels)
        if group in histogram_groups:
            raise ExpositionError(
                f"duplicate histogram: {family.name}{_format_labels(labels)}"
            )
        histogram_groups.add(group)
        observations: list[Decimal] = []
        for observation in histogram.observations:
            if not isinstance(observation, Decimal) or not observation.is_finite():
                raise ExpositionError(
                    "histogram observations must be finite Decimal values"
                )
            observations.append(observation)
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
                canonical_decimal(sum(observations, start=Decimal(0))),
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
    if not _VALUE.fullmatch(text):
        raise ExpositionError(f"noncanonical or nonfinite metric value: {text!r}")
    try:
        value = Decimal(text)
    except InvalidOperation as error:
        raise ExpositionError(f"invalid metric value: {text!r}") from error
    if not value.is_finite() or canonical_decimal(value) != text:
        raise ExpositionError(f"noncanonical or nonfinite metric value: {text!r}")
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


def validate_exposition(payload: bytes) -> int:
    """Strictly parse the supported exposition subset and return unique series count."""
    if not isinstance(payload, bytes):
        raise ExpositionError("exposition must be bytes")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ExpositionError("exposition must be valid UTF-8") from error
    if not text.endswith("\n") or text.endswith("\n\n") or "\r" in text:
        raise ExpositionError("exposition must have exactly one final LF")
    lines = text[:-1].split("\n")
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
            raise ExpositionError(f"malformed sample line: {line!r}")
        name = match.group("name")
        labels = _parse_labels(match.group("labels"))
        value = _parse_value(match.group("value"))
        family, component = _sample_family(name)
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
