from __future__ import annotations

from bisect import bisect_right
from collections import OrderedDict
from dataclasses import dataclass, field
import math
import re
from typing import Any

import numpy as np


@dataclass(frozen=True)
class DatasetView:
    id: str
    title: str
    dataset: Any
    source: Any
    is_eis: bool = False


@dataclass(frozen=True)
class TemperatureSample:
    elapsed_s: float
    temperature_c: float
    setpoint_c: float


@dataclass(frozen=True)
class MeasurementSegment:
    index: int
    label: str
    source: Any
    elapsed_offset_s: float = 0.0
    source_step_index: int | None = None
    step_type: str | None = None
    execution_index: int | None = None
    temperature_samples: tuple[TemperatureSample, ...] = ()


@dataclass(frozen=True)
class AuroraStepCompleted:
    segment: MeasurementSegment


@dataclass(frozen=True)
class LiveMeasurementStarted:
    run_title: str | None = None
    segment: MeasurementSegment | None = None


@dataclass
class LogicalMeasurementRun:
    title: str
    segments: list[MeasurementSegment] = field(default_factory=list)

    def add_segment(self, segment: MeasurementSegment):
        self.segments.append(segment)

    @property
    def measurements(self) -> list[Any]:
        return [segment.source for segment in self.segments if segment.source is not None]


@dataclass(frozen=True)
class UnifiedDataArray:
    name: str
    unit: str
    values: tuple[Any, ...]
    type: str = ""
    quantity: str = ""

    def to_numpy(self):
        return np.asarray(self.values)


@dataclass(frozen=True)
class UnifiedDataset:
    title: str
    _arrays: tuple[UnifiedDataArray, ...]

    def arrays(self):
        return self._arrays


def measurement_dataset_views(
    measurement,
    include_subscans: bool = False,
    include_individual_measurements: bool = False,
    include_individual_eis: bool = True,
    include_unified_eis: bool = True,
) -> list[DatasetView]:
    if isinstance(measurement, LogicalMeasurementRun):
        return _logical_measurement_dataset_views(
            measurement,
            include_subscans=include_subscans,
            include_individual_measurements=include_individual_measurements,
            include_individual_eis=include_individual_eis,
            include_unified_eis=include_unified_eis,
        )

    views: list[DatasetView] = []

    for index, eis_data in enumerate(_eis_data_items(measurement), start=1):
        dataset = _get_dataset(eis_data)
        if _has_arrays(dataset):
            title = _get_title(eis_data, f"EIS {index}")
            views.append(DatasetView(f"eis_{index}", title, dataset, eis_data, is_eis=True))

        if include_subscans:
            for subscan_index, subscan in enumerate(_subscan_items(eis_data), start=1):
                subscan_dataset = _get_dataset(subscan)
                if _has_arrays(subscan_dataset):
                    title = _get_title(subscan, f"EIS {index} subscan {subscan_index}")
                    view_id = f"eis_{index}_subscan_{subscan_index}"
                    views.append(DatasetView(view_id, title, subscan_dataset, subscan, is_eis=True))

    dataset = _get_dataset(measurement)
    if _has_arrays(dataset):
        views.append(DatasetView("measurement", _get_title(measurement, "Measurement"), dataset, measurement))

    return views


def primary_measurement_dataset(measurement):
    views = measurement_dataset_views(measurement)
    if not views:
        return None
    for view in views:
        if not view.is_eis:
            return view.dataset
    return views[0].dataset


def measurement_arrays(measurement) -> list[Any]:
    dataset = primary_measurement_dataset(measurement)
    if dataset is None:
        return []
    return dataset_arrays(dataset)


def dataset_arrays(dataset) -> list[Any]:
    arrays = getattr(dataset, "arrays", None)
    if callable(arrays):
        return list(arrays())

    values = getattr(dataset, "values", None)
    if callable(values):
        return list(values())

    try:
        return [dataset[key] for key in dataset]
    except TypeError:
        return []


def default_axis_indexes(arrays: list[Any]) -> tuple[int, int]:
    if not arrays:
        return 0, 0

    x_index = _find_array_index(arrays, ("Frequency",), ("frequency", "freq", "f"))
    y_index = _find_array_index(arrays, ("Z",), ("z", "impedance", "modulus"))

    if x_index is not None and y_index is not None and x_index != y_index:
        return x_index, y_index

    x_index = _find_array_index(arrays, ("ZRe",), ("zre", "zreal", "z'", "z`"))
    y_index = _find_array_index(arrays, ("ZIm",), ("zim", "zimag", "z''", "z``"))

    if x_index is not None and y_index is not None and x_index != y_index:
        return x_index, y_index

    return 0, min(1, len(arrays) - 1)


def _get_dataset(source):
    if source is None:
        return None
    return getattr(source, "dataset", None)


def _has_arrays(dataset) -> bool:
    if dataset is None:
        return False
    return bool(dataset_arrays(dataset))


def _get_title(source, default: str) -> str:
    title = getattr(source, "title", None)
    return str(title) if title else default


def _eis_data_items(measurement) -> list[Any]:
    for attribute in ("eis_data", "eisdata"):
        eis_data = getattr(measurement, attribute, None)
        if eis_data:
            return list(eis_data)
    return []


def _subscan_items(eis_data) -> list[Any]:
    subscans = getattr(eis_data, "subscans", None)
    if subscans:
        return list(subscans)
    return []


def _logical_measurement_dataset_views(
    measurement: LogicalMeasurementRun,
    *,
    include_subscans: bool = False,
    include_individual_measurements: bool = False,
    include_individual_eis: bool = True,
    include_unified_eis: bool = True,
) -> list[DatasetView]:
    views: list[DatasetView] = []

    normal_sources = [
        (segment, _get_dataset(segment.source))
        for segment in measurement.segments
        if _get_dataset(segment.source) is not None
        and not _eis_data_items(segment.source)
    ]
    normal_dataset = _unify_segment_datasets(f"{measurement.title} measurement", normal_sources)
    if _has_arrays(normal_dataset):
        views.append(DatasetView("measurement", measurement.title, normal_dataset, measurement))

    if include_individual_measurements:
        for segment, dataset in normal_sources:
            title = _segment_title(segment)
            individual_dataset = _unify_segment_datasets(title, [(segment, dataset)])
            if _has_arrays(individual_dataset):
                execution_index = segment.execution_index or segment.index
                views.append(
                    DatasetView(
                        f"step_{execution_index}",
                        title,
                        individual_dataset,
                        segment.source,
                    )
                )

    eis_sources = []
    individual_eis_sources = []
    for segment in measurement.segments:
        for eis_index, eis_data in enumerate(_eis_data_items(segment.source), start=1):
            dataset = _get_dataset(eis_data)
            if dataset is not None:
                eis_sources.append((segment, dataset))
                individual_eis_sources.append(
                    (
                        segment,
                        dataset,
                        eis_data,
                        f"eis_step_{segment.index}_{eis_index}",
                        _segment_eis_title(segment, eis_index),
                    )
                )

            if include_subscans:
                for subscan_index, subscan in enumerate(_subscan_items(eis_data), start=1):
                    subscan_dataset = _get_dataset(subscan)
                    if subscan_dataset is not None:
                        eis_sources.append((segment, subscan_dataset))
                        individual_eis_sources.append(
                            (
                                segment,
                                subscan_dataset,
                                subscan,
                                f"eis_step_{segment.index}_{eis_index}_subscan_{subscan_index}",
                                f"{_segment_eis_title(segment, eis_index)} subscan {subscan_index}",
                            )
                        )

    if include_individual_eis:
        for segment, dataset, source, view_id, title in individual_eis_sources:
            individual_dataset = _unify_segment_datasets(title, [(segment, dataset)])
            if _has_arrays(individual_dataset):
                views.append(DatasetView(view_id, title, individual_dataset, source, is_eis=True))

    eis_dataset = (
        _unify_segment_datasets(
            f"{measurement.title} EIS",
            eis_sources,
            separate_sources=True,
        )
        if include_unified_eis
        else None
    )
    if include_unified_eis and _has_arrays(eis_dataset):
        views.append(DatasetView("eis", f"{measurement.title} EIS", eis_dataset, measurement, is_eis=True))

    return views


def _segment_eis_title(segment: MeasurementSegment, eis_index: int) -> str:
    step_id = segment.source_step_index if segment.source_step_index is not None else segment.index
    step_type = segment.step_type or segment.label or "EIS"
    suffix = f" EIS {eis_index}" if eis_index > 1 else " EIS"
    return f"Step {step_id}: {step_type}{suffix}"


def _segment_title(segment: MeasurementSegment) -> str:
    execution_index = segment.execution_index or segment.index
    step_type = (segment.step_type or segment.label or "step").replace("_", " ")
    return f"{execution_index} · {step_type}"


def _unify_segment_datasets(
    title: str,
    sources: list[tuple[MeasurementSegment, Any]],
    *,
    separate_sources: bool = False,
) -> UnifiedDataset | None:
    values_by_key: OrderedDict[tuple[str, str, str, str], list[Any]] = OrderedDict()
    metadata_by_key: dict[tuple[str, str, str, str], dict[str, str]] = {}
    segment_indexes: list[int] = []
    step_ids: list[Any] = []
    execution_indexes: list[int] = []
    step_types: list[str] = []
    total_length = 0

    for segment, dataset in sources:
        arrays = dataset_arrays(dataset)
        if not arrays:
            continue

        row_count = _dataset_row_count(arrays)
        if row_count <= 0:
            continue

        if separate_sources and total_length:
            for values in values_by_key.values():
                values.append(math.nan)
            segment_indexes.append(math.nan)
            step_ids.append(math.nan)
            execution_indexes.append(math.nan)
            step_types.append("")
            total_length += 1

        source_values: dict[tuple[str, str, str, str], list[Any]] = {}
        source_priorities: dict[tuple[str, str, str, str], int] = {}
        for array_index, data_array in enumerate(arrays):
            key = _data_array_identity(data_array, array_index)
            metadata_by_key.setdefault(key, _data_array_metadata(data_array, key))
            values = _array_values(data_array)
            if _is_time_array(data_array):
                values = _offset_numeric_values(values, segment.elapsed_offset_s)
            fitted_values = _fit_values(values, row_count)
            priority = _data_array_priority(data_array)
            if key not in source_values or priority < source_priorities[key]:
                source_values[key] = fitted_values
                source_priorities[key] = priority

        for data_array in _segment_temperature_arrays(segment, arrays, row_count):
            key = _data_array_identity(data_array, len(source_values))
            metadata_by_key.setdefault(key, _data_array_metadata(data_array, key))
            source_values[key] = _array_values(data_array)
            source_priorities[key] = 1

        for key in values_by_key:
            if key not in source_values:
                values_by_key[key].extend(_missing_values(row_count))

        for key, values in source_values.items():
            if key not in values_by_key:
                values_by_key[key] = _missing_values(total_length)
            values_by_key[key].extend(values)

        segment_indexes.extend([segment.index] * row_count)
        execution_indexes.extend([(segment.execution_index or segment.index)] * row_count)
        step_ids.extend([segment.source_step_index if segment.source_step_index is not None else segment.index] * row_count)
        step_types.extend([segment.step_type or ""] * row_count)
        total_length += row_count

    if total_length == 0:
        return None

    arrays = [
        UnifiedDataArray(
            name=metadata["name"],
            unit=metadata["unit"],
            type=metadata["type"],
            quantity=metadata["quantity"],
            values=tuple(values),
        )
        for key, values in values_by_key.items()
        for metadata in (metadata_by_key[key],)
    ]
    arrays.extend(
        [
            UnifiedDataArray("segment_index", "", tuple(segment_indexes), type="Count", quantity="segment_index"),
            UnifiedDataArray("step_id", "", tuple(step_ids), type="Count", quantity="step_id"),
            UnifiedDataArray("execution_index", "", tuple(execution_indexes), type="Count", quantity="execution_index"),
            UnifiedDataArray("step_type", "", tuple(step_types), type="String", quantity="step_type"),
        ]
    )
    return UnifiedDataset(title, tuple(arrays))


def _data_array_identity(data_array, index: int) -> tuple[str, str, str, str]:
    metadata = _data_array_metadata(data_array, (str(index), "", "", ""))
    return (
        metadata["name"],
        metadata["unit"],
        metadata["type"],
        metadata["quantity"],
    )


def _data_array_metadata(data_array, fallback_key) -> dict[str, str]:
    fallback_name = fallback_key[0] if isinstance(fallback_key, tuple) else str(fallback_key)
    raw_name = str(getattr(data_array, "name", "") or getattr(data_array, "quantity", "") or fallback_name)
    raw_type = str(getattr(data_array, "type", "") or "")
    raw_quantity = str(getattr(data_array, "quantity", "") or "")
    canonical = _canonical_array_name(raw_name, raw_type, raw_quantity)
    name = canonical or raw_name
    return {
        "name": name,
        "unit": str(getattr(data_array, "unit", "") or ""),
        "type": canonical or raw_type,
        "quantity": canonical.casefold() if canonical else raw_quantity,
    }


def _dataset_row_count(arrays: list[Any]) -> int:
    lengths = [len(_array_values(data_array)) for data_array in arrays]
    return max(lengths, default=0)


def _array_values(data_array) -> list[Any]:
    to_numpy = getattr(data_array, "to_numpy", None)
    if callable(to_numpy):
        values = to_numpy()
    else:
        values = getattr(data_array, "values", [])

    array = np.asarray(values).ravel()
    return array.tolist()


def _segment_temperature_arrays(
    segment: MeasurementSegment,
    source_arrays: list[Any],
    row_count: int,
) -> list[UnifiedDataArray]:
    samples = segment.temperature_samples
    if not samples:
        return []

    elapsed_values = _measurement_elapsed_values(source_arrays, row_count)
    return [
        UnifiedDataArray(
            name="ChamberTemperature",
            unit="degC",
            type="Temperature",
            quantity="chamber_temperature",
            values=tuple(
                _align_temperature_samples(samples, elapsed_values, "temperature_c", row_count)
            ),
        ),
        UnifiedDataArray(
            name="ChamberSetpoint",
            unit="degC",
            type="TemperatureT1",
            quantity="temperature_t1",
            values=tuple(
                _align_temperature_samples(samples, elapsed_values, "setpoint_c", row_count)
            ),
        ),
    ]


def _measurement_elapsed_values(source_arrays: list[Any], row_count: int) -> list[float] | None:
    time_array = next((data_array for data_array in source_arrays if _is_time_array(data_array)), None)
    if time_array is None:
        return None

    try:
        return [float(value) for value in _fit_values(_array_values(time_array), row_count)]
    except (TypeError, ValueError):
        return None


def _align_temperature_samples(
    samples: tuple[TemperatureSample, ...],
    elapsed_values: list[float] | None,
    attribute: str,
    row_count: int,
) -> list[float]:
    sample_values = [float(getattr(sample, attribute)) for sample in samples]
    if elapsed_values is None:
        return [sample_values[-1]] * row_count

    sample_times = [sample.elapsed_s for sample in samples]
    return [
        sample_values[max(0, bisect_right(sample_times, elapsed_s) - 1)]
        for elapsed_s in elapsed_values
    ]


def _fit_values(values: list[Any], length: int) -> list[Any]:
    if len(values) == length:
        return values
    if len(values) == 1 and length > 1:
        return values * length
    if len(values) > length:
        return values[:length]
    return values + _missing_values(length - len(values))


def _missing_values(length: int) -> list[float]:
    return [math.nan] * length


def _offset_numeric_values(values: list[Any], offset: float) -> list[Any]:
    adjusted = []
    for value in values:
        try:
            adjusted.append(float(value) + offset)
        except (TypeError, ValueError):
            adjusted.append(value)
    return adjusted


def _is_time_array(data_array) -> bool:
    texts = (
        getattr(data_array, "name", ""),
        getattr(data_array, "type", ""),
        getattr(data_array, "quantity", ""),
    )
    normalized_texts = {_normalize_text(text) for text in texts}
    unit = _normalize_text(getattr(data_array, "unit", ""))
    return (
        unit in {"s", "sec", "second", "seconds"}
        and bool(normalized_texts.intersection({"time", "testtime", "elapsedtime"}))
    ) or bool(normalized_texts.intersection({"time", "testtime", "elapsedtime"}))


def _canonical_array_name(name: str, array_type: str, quantity: str) -> str | None:
    texts = (
        _strip_palmsens_suffix(name),
        _strip_palmsens_suffix(array_type),
        _strip_palmsens_suffix(quantity),
    )
    normalized = {_normalize_text(text) for text in texts if text}

    if normalized.intersection({"time", "testtime", "elapsedtime"}):
        return "Time"
    if normalized.intersection({"potentialwevsce", "wevscepotential"}):
        return "PotentialWEvsCE"
    if normalized.intersection({"potential", "appliedpotential", "voltage", "appliedvoltage"}):
        return "Potential"
    if normalized.intersection({"current", "appliedcurrent"}):
        return "Current"
    if normalized.intersection({"frequency", "freq"}):
        return "Frequency"
    if normalized.intersection({"zreal", "realimpedance"}):
        return "ZReal"
    if normalized.intersection({"zimag", "imaginaryimpedance"}):
        return "ZImag"
    if normalized.intersection({"phase", "phaseangle"}):
        return "Phase"
    if normalized.intersection({"impedance", "z", "absoluteimpedance"}):
        return "Impedance"

    return None


def _strip_palmsens_suffix(value: str) -> str:
    return re.sub(r"\d+_\d+$", "", str(value or ""))


def _data_array_priority(data_array) -> int:
    name = _normalize_text(_strip_palmsens_suffix(getattr(data_array, "name", "")))
    if name.startswith("applied"):
        return 2
    return 1


def _find_array_index(arrays: list[Any], type_names: tuple[str, ...], text_names: tuple[str, ...]) -> int | None:
    normalized_types = {_normalize_text(name) for name in type_names}
    normalized_texts = {_normalize_text(name) for name in text_names}

    for index, data_array in enumerate(arrays):
        array_type = _normalize_text(getattr(data_array, "type", ""))
        if array_type in normalized_types:
            return index

    for index, data_array in enumerate(arrays):
        texts = (
            _normalize_text(getattr(data_array, "name", "")),
            _normalize_text(getattr(data_array, "quantity", "")),
        )
        if any(text in normalized_texts for text in texts):
            return index

    return None


def _normalize_text(value) -> str:
    if value is None:
        return ""
    return "".join(character for character in str(value).casefold() if character.isalnum())
