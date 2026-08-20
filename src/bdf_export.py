import math
import re
from dataclasses import dataclass
from pathlib import Path

import bdf
import pandas as pd

from src.measurement_data import dataset_arrays, measurement_dataset_views


_GROUPED_KEY_PATTERN = re.compile(r"^(?P<base>.+?)_(?P<group>\d+)$")

_TIME_FACTORS = {
    "s": 1.0,
    "sec": 1.0,
    "second": 1.0,
    "seconds": 1.0,
    "ms": 1e-3,
    "millisecond": 1e-3,
    "milliseconds": 1e-3,
    "min": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hrs": 3600.0,
    "hour": 3600.0,
    "hours": 3600.0,
}

_VOLTAGE_FACTORS = {
    "v": 1.0,
    "volt": 1.0,
    "volts": 1.0,
    "mv": 1e-3,
    "millivolt": 1e-3,
    "millivolts": 1e-3,
}

_CURRENT_FACTORS = {
    "a": 1.0,
    "amp": 1.0,
    "amps": 1.0,
    "ampere": 1.0,
    "amperes": 1.0,
    "ma": 1e-3,
    "milliamp": 1e-3,
    "milliamps": 1e-3,
    "milliampere": 1e-3,
    "milliamperes": 1e-3,
    "ua": 1e-6,
    "microamp": 1e-6,
    "microamps": 1e-6,
    "microampere": 1e-6,
    "microamperes": 1e-6,
    "na": 1e-9,
    "nanoamp": 1e-9,
    "nanoamps": 1e-9,
    "nanoampere": 1e-9,
    "nanoamperes": 1e-9,
}

_RESISTANCE_FACTORS = {
    "ohm": 1.0,
    "ohms": 1.0,
    "omega": 1.0,
    "kohm": 1e3,
    "kiloohm": 1e3,
    "mohm": 1e-3,
    "milliohm": 1e-3,
    "uohm": 1e-6,
    "microohm": 1e-6,
}

_PRESSURE_FACTORS = {
    "pa": 1.0,
    "pascal": 1.0,
    "pascals": 1.0,
    "kpa": 1e3,
    "kilopascal": 1e3,
    "kilopascals": 1e3,
    "mpa": 1e6,
    "megapascal": 1e6,
    "megapascals": 1e6,
    "bar": 1e5,
    "mbar": 100.0,
}

_TEMPERATURE_FACTORS = {
    "degc": 1.0,
    "c": 1.0,
    "celsius": 1.0,
    "degreecelsius": 1.0,
    "degreescelsius": 1.0,
}

_CAPACITY_FACTORS = {
    "ah": 1.0,
    "amperehour": 1.0,
    "amperehours": 1.0,
    "mah": 1e-3,
    "milliamperehour": 1e-3,
    "milliamperehours": 1e-3,
    "uah": 1e-6,
    "microamperehour": 1e-6,
    "microamperehours": 1e-6,
}

_ENERGY_FACTORS = {
    "wh": 1.0,
    "watthour": 1.0,
    "watthours": 1.0,
    "mwh": 1e-3,
    "milliwatthour": 1e-3,
    "milliwatthours": 1e-3,
    "kwh": 1e3,
    "kilowatthour": 1e3,
    "kilowatthours": 1e3,
}

_FREQUENCY_FACTORS = {
    "hz": 1.0,
    "hertz": 1.0,
    "khz": 1e3,
    "kilohertz": 1e3,
    "mhz": 1e6,
    "megahertz": 1e6,
}

_ANGLE_FACTORS = {
    "deg": 1.0,
    "degree": 1.0,
    "degrees": 1.0,
}

_POWER_FACTORS = {
    "w": 1.0,
    "watt": 1.0,
    "watts": 1.0,
    "mw": 1e-3,
    "milliwatt": 1e-3,
    "milliwatts": 1e-3,
    "kw": 1e3,
    "kilowatt": 1e3,
    "kilowatts": 1e3,
}

_COUNT_FACTORS = {
    "": 1.0,
    "1": 1.0,
    "one": 1.0,
    "unitone": 1.0,
    "dimensionless": 1.0,
}


@dataclass(frozen=True)
class _BdfTerm:
    key: str
    label: str
    aliases: tuple[str, ...]
    unit_factors: dict[str, float] | None = None
    value_type: str = "float"
    required: bool = False


def _term(
    key: str,
    label: str,
    aliases: tuple[str, ...],
    unit_factors: dict[str, float] | None = None,
    value_type: str = "float",
    required: bool = False,
) -> _BdfTerm:
    return _BdfTerm(key, label, aliases + (key, label), unit_factors, value_type, required)


def _normalize_text(value) -> str:
    if value is None:
        return ""
    return re.sub(r"[^a-z0-9]+", "", str(value).casefold())


_BDF_TERMS = (
    _term("ac_internal_resistance_ohm", "AC Internal Resistance / ohm", ("ac_resistance_ohm", "acr"), _RESISTANCE_FACTORS),
    _term("absolute_impedance_ohm", "Absolute Impedance / ohm", ("impedance", "impedance_modulus", "z", "z_abs", "abs_z"), _RESISTANCE_FACTORS),
    _term("ambient_pressure_pa", "Ambient Pressure / Pa", ("ambient_pressure_pascal", "ambient_pressure", "ambient_air_pressure"), _PRESSURE_FACTORS),
    _term("ambient_temperature_celsius", "Ambient Temperature / degC", ("ambient_temperature_degc", "chamber_temperature", "environment_temperature"), _TEMPERATURE_FACTORS),
    _term("applied_pressure_pa", "Applied Pressure / Pa", ("applied_pressure_pascal", "applied_pressure"), _PRESSURE_FACTORS),
    _term("charging_capacity_ah", "Charging Capacity / Ah", ("charging_capacity_ampere_hour", "charge_capacity_ah"), _CAPACITY_FACTORS),
    _term("charging_energy_wh", "Charging Energy / Wh", ("charging_energy_watt_hour", "charge_energy_wh"), _ENERGY_FACTORS),
    _term("cumulative_capacity_ah", "Cumulative Capacity / Ah", ("cumulative_capacity_ampere_hour", "total_capacity_ah", "throughput_capacity_ah"), _CAPACITY_FACTORS),
    _term("cumulative_energy_wh", "Cumulative Energy / Wh", ("cumulative_energy_watt_hour", "total_energy_wh", "throughput_energy_wh"), _ENERGY_FACTORS),
    _term("current_ampere", "Current / A", ("current_a", "current", "i"), _CURRENT_FACTORS, required=True),
    _term("cycle_charging_capacity_ah", "Cycle Charging Capacity / Ah", ("cycle_charge_capacity_ah", "cycle_charging_capacity_ampere_hour"), _CAPACITY_FACTORS),
    _term("cycle_charging_energy_wh", "Cycle Charging Energy / Wh", ("cycle_charge_energy_wh", "cycle_charging_energy_watt_hour"), _ENERGY_FACTORS),
    _term("cycle_count", "Cycle Count / 1", ("cycle_count_1", "cycle_dimensionless", "cycle_number", "cycle"), _COUNT_FACTORS),
    _term("cycle_cumulative_capacity_ah", "Cycle Cumulative Capacity / Ah", ("cycle_cumulative_capacity_ampere_hour",), _CAPACITY_FACTORS),
    _term("cycle_cumulative_energy_wh", "Cycle Cumulative Energy / Wh", ("cycle_cumulative_energy_watt_hour",), _ENERGY_FACTORS),
    _term("cycle_discharging_capacity_ah", "Cycle Discharging Capacity / Ah", ("cycle_discharge_capacity_ah", "cycle_discharging_capacity_ampere_hour"), _CAPACITY_FACTORS),
    _term("cycle_discharging_energy_wh", "Cycle Discharging Energy / Wh", ("cycle_discharge_energy_wh", "cycle_discharging_energy_watt_hour"), _ENERGY_FACTORS),
    _term("cycle_net_capacity_ah", "Cycle Net Capacity / Ah", ("cycle_net_capacity_ampere_hour",), _CAPACITY_FACTORS),
    _term("cycle_net_energy_wh", "Cycle Net Energy / Wh", ("cycle_net_energy_watt_hour",), _ENERGY_FACTORS),
    _term("dc_internal_resistance_ohm", "DC Internal Resistance / ohm", ("dc_resistance_ohm", "dcir", "dcir_ohm"), _RESISTANCE_FACTORS),
    _term("discharging_capacity_ah", "Discharging Capacity / Ah", ("discharging_capacity_ampere_hour", "discharge_capacity_ah"), _CAPACITY_FACTORS),
    _term("discharging_energy_wh", "Discharging Energy / Wh", ("discharging_energy_watt_hour", "discharge_energy_wh"), _ENERGY_FACTORS),
    _term("frequency_hertz", "Frequency / Hz", ("frequency", "freq", "f"), _FREQUENCY_FACTORS),
    _term("imaginary_impedance_ohm", "Imaginary Impedance / ohm", ("z_imag", "zimag", "z_im", "zim", "z''", "z``"), _RESISTANCE_FACTORS),
    _term("internal_resistance_ohm", "Internal Resistance / ohm", ("internal_resistance", "resistance", "r_int"), _RESISTANCE_FACTORS),
    _term("net_capacity_ah", "Net Capacity / Ah", ("net_capacity_ampere_hour", "q_q0"), _CAPACITY_FACTORS),
    _term("net_energy_wh", "Net Energy / Wh", ("net_energy_watt_hour",), _ENERGY_FACTORS),
    _term("phase_degree", "Phase / deg", ("phase", "phase_angle", "phase_deg"), _ANGLE_FACTORS),
    _term("power_watt", "Power / W", ("power_w", "power", "p"), _POWER_FACTORS),
    _term("real_impedance_ohm", "Real Impedance / ohm", ("z_real", "zreal", "z_re", "zre", "z'", "z`"), _RESISTANCE_FACTORS),
    _term("record_index", "Record Index / 1", ("record", "record_number", "data_point", "data_point_index"), _COUNT_FACTORS),
    _term("step_charging_capacity_ah", "Step Charging Capacity / Ah", ("step_charge_capacity_ah", "step_charging_capacity_ampere_hour", "q_charge"), _CAPACITY_FACTORS),
    _term("step_charging_energy_wh", "Step Charging Energy / Wh", ("step_charge_energy_wh", "step_charging_energy_watt_hour"), _ENERGY_FACTORS),
    _term("step_count", "Step Count / 1", ("step_count_1", "step_dimensionless", "step_number"), _COUNT_FACTORS),
    _term("step_cumulative_capacity_ah", "Step Cumulative Capacity / Ah", ("step_capacity_ah", "step_cumulative_capacity_ampere_hour"), _CAPACITY_FACTORS),
    _term("step_cumulative_energy_wh", "Step Cumulative Energy / Wh", ("step_energy_wh", "step_cumulative_energy_watt_hour"), _ENERGY_FACTORS),
    _term("step_discharging_capacity_ah", "Step Discharging Capacity / Ah", ("step_discharge_capacity_ah", "step_discharging_capacity_ampere_hour", "q_discharge"), _CAPACITY_FACTORS),
    _term("step_discharging_energy_wh", "Step Discharging Energy / Wh", ("step_discharge_energy_wh", "step_discharging_energy_watt_hour"), _ENERGY_FACTORS),
    _term("step_id", "Step ID", ("step_index_in_program", "program_step", "ns"), _COUNT_FACTORS),
    _term("step_index", "Step Index / 1", ("step_index_1", "within_step_index"), _COUNT_FACTORS),
    _term("step_net_capacity_ah", "Step Net Capacity / Ah", ("step_net_capacity_ampere_hour",), _CAPACITY_FACTORS),
    _term("step_net_energy_wh", "Step Net Energy / Wh", ("step_net_energy_watt_hour",), _ENERGY_FACTORS),
    _term("step_time_second", "Step Time / s", ("step_time_s", "step_time", "steptime", "step_elapsed_time"), _TIME_FACTORS),
    _term("step_type", "Step Type", ("mode", "operation_mode"), None, "string"),
    _term("surface_pressure_pa", "Surface Pressure / Pa", ("surface_pressure_pascal", "surface_pressure", "skin_pressure"), _PRESSURE_FACTORS),
    _term("surface_temperature_celsius", "Surface Temperature / degC", ("surface_temperature_degc", "surface_temperature", "skin_temperature"), _TEMPERATURE_FACTORS),
    _term("temperature_t1_celsius", "Temperature T1 / degC", ("surface_temperature_t1_celsius", "temperature_t1_degc", "temperature_t1", "temperature", "temp", "t1"), _TEMPERATURE_FACTORS),
    _term("temperature_t2_celsius", "Temperature T2 / degC", ("surface_temperature_t2_celsius", "temperature_t2_degc", "temperature_t2", "t2"), _TEMPERATURE_FACTORS),
    _term("temperature_t3_celsius", "Temperature T3 / degC", ("surface_temperature_t3_celsius", "temperature_t3_degc", "temperature_t3", "t3"), _TEMPERATURE_FACTORS),
    _term("temperature_t4_celsius", "Temperature T4 / degC", ("surface_temperature_t4_celsius", "temperature_t4_degc", "temperature_t4", "t4"), _TEMPERATURE_FACTORS),
    _term("temperature_t5_celsius", "Temperature T5 / degC", ("surface_temperature_t5_celsius", "temperature_t5_degc", "temperature_t5", "t5"), _TEMPERATURE_FACTORS),
    _term("test_time_second", "Test Time / s", ("test_time_s", "test_time", "testtime", "elapsed_time", "elapsedtime", "time"), _TIME_FACTORS, required=True),
    _term("unix_time_second", "Unix Time / s", ("unix_time_s", "unix_time", "timestamp", "epoch_time"), _TIME_FACTORS),
    _term("voltage_volt", "Voltage / V", ("voltage_v", "voltage", "potential", "e", "v"), _VOLTAGE_FACTORS, required=True),
    _term("voltage_wevsce_volt", "Voltage Full Cell / V", ("full_cell_voltage", "full_cell", "potentialwevsce"), _VOLTAGE_FACTORS),
)

_BDF_TERMS_BY_KEY = {term.key: term for term in _BDF_TERMS}
_REQUIRED_BDF_KEYS = tuple(term.key for term in _BDF_TERMS if term.required)
_CUSTOM_BDF_KEYS = {"voltage_wevsce_volt"}
_WEVSCE_IDENTIFIERS = {"potentialwevsce", "voltagewevsce", "wevsce"}
_STEP_DERIVED_KEYS = {
    "step_charging_capacity_ah",
    "step_discharging_capacity_ah",
    "step_cumulative_capacity_ah",
    "step_net_capacity_ah",
    "step_charging_energy_wh",
    "step_discharging_energy_wh",
    "step_cumulative_energy_wh",
    "step_net_energy_wh",
}
_CYCLE_DERIVED_KEYS = {
    "cycle_charging_capacity_ah",
    "cycle_discharging_capacity_ah",
    "cycle_cumulative_capacity_ah",
    "cycle_net_capacity_ah",
    "cycle_charging_energy_wh",
    "cycle_discharging_energy_wh",
    "cycle_cumulative_energy_wh",
    "cycle_net_energy_wh",
}
_BDF_ALIAS_TO_KEY: dict[str, str] = {}
for _term_definition in _BDF_TERMS:
    for _alias in _term_definition.aliases:
        _BDF_ALIAS_TO_KEY.setdefault(_normalize_text(_alias), _term_definition.key)


class BdfExportError(ValueError):
    pass


def export_measurement_to_bdf_files(
    measurement,
    output_dir: Path,
    filename_stem: str,
    export_type: str,
    optional_quantity_keys: set[str] | None = None,
) -> list[Path]:
    dataset_views = measurement_dataset_views(measurement, include_unified_eis=False)
    if not dataset_views:
        raise BdfExportError("Measurement does not contain any dataset arrays.")

    dataframes = []
    export_errors = []

    for dataset_view in dataset_views:
        groups = _measurement_groups(dataset_view.dataset)
        for group in groups:
            try:
                series = _extract_bdf_series(group, optional_quantity_keys)
            except BdfExportError as exc:
                if len(dataset_views) == 1:
                    raise
                export_errors.append(str(exc))
                continue

            dataframe = _build_dataframe(series)
            dataframes.append(dataframe)
            known_bdf_labels = {term.label for term in _BDF_TERMS if term.key not in _CUSTOM_BDF_KEYS}
            validation_df = dataframe[[col for col in dataframe.columns if col in known_bdf_labels]]
            res = bdf.validate(validation_df, raise_on_error=True)
            if not res["ok"]:
                raise BdfExportError("Invalid battery data format dataframe")
    if not dataframes:
        if export_errors:
            raise BdfExportError(export_errors[0])
        raise BdfExportError("Measurement does not contain any exportable dataset groups.")

    combined_dataframes = _sort_bdf_dataframe(pd.concat(dataframes, ignore_index=True))
    total_path = _unique_output_path(output_dir, filename_stem, export_type)
    _write_dataframe(total_path, combined_dataframes, export_type)
    return [total_path]


def bdf_optional_quantity_choices() -> list[tuple[str, str]]:
    return [(term.key, term.label) for term in _BDF_TERMS if not term.required]


def _measurement_groups(dataset):
    groups = {}

    for index, data_array in enumerate(dataset_arrays(dataset)):
        key = _data_array_key(data_array, index)
        match = _GROUPED_KEY_PATTERN.match(key)
        if match:
            base_key = match.group("base")
            group_id = match.group("group")
        else:
            base_key = key
            group_id = "default"

        group = groups.setdefault(
            group_id,
            {
                "id": group_id,
                "arrays": [],
            },
        )
        group["arrays"].append(
            {
                "key": key,
                "base_key": base_key,
                "array": data_array,
            }
        )

    return sorted(
        groups.values(),
        key=lambda group: (
            group["id"] == "default",
            int(group["id"]) if str(group["id"]).isdigit() else float("inf"),
            str(group["id"]),
        ),
    )


def _data_array_key(data_array, index: int) -> str:
    for attribute_name in ("name", "quantity", "type"):
        value = getattr(data_array, attribute_name, None)
        if value:
            return str(value)
    return str(index)


def _extract_bdf_series(group, optional_quantity_keys: set[str] | None = None) -> dict[str, list]:
    series = {}
    raw_dependency_keys = _raw_dependency_keys(optional_quantity_keys)

    for entry in group["arrays"]:
        data_array = entry["array"]
        base_key = entry["base_key"]
        array_name = getattr(data_array, "name", base_key)
        array_type = getattr(data_array, "type", "")
        quantity = getattr(data_array, "quantity", "")
        unit = getattr(data_array, "unit", "")
        values = data_array.to_numpy()
        column_key = _detect_bdf_column(base_key, array_name, array_type, quantity, unit)

        if column_key is None or column_key in series:
            continue
        if optional_quantity_keys is not None:
            if column_key not in raw_dependency_keys:
                continue

        text_values = (base_key, array_name, array_type, quantity)
        series[column_key] = _convert_values(column_key, unit, values, text_values)

    missing_columns = [column_key for column_key in _REQUIRED_BDF_KEYS if column_key not in series]
    if missing_columns:
        _add_open_circuit_current(series, group)
        missing_columns = [column_key for column_key in _REQUIRED_BDF_KEYS if column_key not in series]

    if missing_columns:
        raise BdfExportError(
            "Missing required BDF quantities. The measurement must include time, voltage, and current arrays."
        )

    _ensure_matching_lengths(series)
    _derive_bdf_series(series, optional_quantity_keys)
    series = _filter_bdf_series(series, optional_quantity_keys)
    _ensure_matching_lengths(series)

    return series


def _add_open_circuit_current(series: dict[str, list], group):
    if "current_ampere" in series:
        return
    if "test_time_second" not in series or "voltage_volt" not in series:
        return
    if not _is_open_circuit_group(group):
        return

    series["current_ampere"] = [0.0] * len(series["test_time_second"])


def _is_open_circuit_group(group) -> bool:
    for entry in group["arrays"]:
        data_array = entry["array"]
        base_key = entry["base_key"]
        array_name = getattr(data_array, "name", base_key)
        array_type = getattr(data_array, "type", "")
        quantity = getattr(data_array, "quantity", "")
        unit = getattr(data_array, "unit", "")
        if _detect_bdf_column(base_key, array_name, array_type, quantity, unit) != "step_type":
            continue

        values = data_array.to_numpy()
        if any(_is_open_circuit_step_type(value) for value in values):
            return True

    return False


def _is_open_circuit_step_type(value) -> bool:
    # Temperature steps are recorded with open-circuit potentiometry.
    normalized = _normalize_text(value)
    return normalized in {"opencircuitvoltage", "ocv", "temperature"}


def _raw_dependency_keys(optional_quantity_keys: set[str] | None) -> set[str]:
    if optional_quantity_keys is None:
        return set(_BDF_TERMS_BY_KEY)

    keys = set(_REQUIRED_BDF_KEYS)
    keys.update(optional_quantity_keys)

    if optional_quantity_keys.intersection({"absolute_impedance_ohm", "phase_degree"}):
        keys.update({"real_impedance_ohm", "imaginary_impedance_ohm"})

    if optional_quantity_keys.intersection({"real_impedance_ohm", "imaginary_impedance_ohm"}):
        keys.update({"absolute_impedance_ohm", "phase_degree"})

    if optional_quantity_keys.intersection(_STEP_DERIVED_KEYS):
        keys.update({"step_count", "step_id", "step_time_second"})

    if optional_quantity_keys.intersection(_CYCLE_DERIVED_KEYS):
        keys.add("cycle_count")

    return keys


def _filter_bdf_series(series: dict[str, list], optional_quantity_keys: set[str] | None) -> dict[str, list]:
    if optional_quantity_keys is None:
        return series

    allowed_keys = set(_REQUIRED_BDF_KEYS)
    allowed_keys.update(optional_quantity_keys)
    return {column_key: values for column_key, values in series.items() if column_key in allowed_keys}


def _ensure_matching_lengths(series: dict[str, list]):
    length = len(series["test_time_second"])
    mismatched_columns = [column_key for column_key, values in series.items() if len(values) != length]
    if mismatched_columns:
        labels = ", ".join(_BDF_TERMS_BY_KEY[column_key].label for column_key in mismatched_columns)
        raise BdfExportError(f"BDF arrays do not have matching lengths: {labels}.")


def _derive_bdf_series(series: dict[str, list], optional_quantity_keys: set[str] | None):
    if _quantity_requested("power_watt", series, optional_quantity_keys) and _has_keys(series, "voltage_volt", "current_ampere"):
        series["power_watt"] = _power_values(series)

    if _has_keys(series, "real_impedance_ohm", "imaginary_impedance_ohm"):
        real = series["real_impedance_ohm"]
        imag = series["imaginary_impedance_ohm"]
        if _quantity_requested("absolute_impedance_ohm", series, optional_quantity_keys):
            series["absolute_impedance_ohm"] = [math.hypot(re, im) for re, im in zip(real, imag)]
        if _quantity_requested("phase_degree", series, optional_quantity_keys):
            series["phase_degree"] = [math.degrees(math.atan2(im, re)) for re, im in zip(real, imag)]

    if (
        not _has_keys(series, "real_impedance_ohm", "imaginary_impedance_ohm")
        and _has_keys(series, "absolute_impedance_ohm", "phase_degree")
    ):
        z_abs = series["absolute_impedance_ohm"]
        phase = series["phase_degree"]
        if _quantity_requested("real_impedance_ohm", series, optional_quantity_keys):
            series["real_impedance_ohm"] = [z * math.cos(math.radians(p)) for z, p in zip(z_abs, phase)]
        if _quantity_requested("imaginary_impedance_ohm", series, optional_quantity_keys):
            series["imaginary_impedance_ohm"] = [z * math.sin(math.radians(p)) for z, p in zip(z_abs, phase)]

    _derive_capacity_series(
        series,
        "",
        None,
        {
            "charging": "charging_capacity_ah",
            "discharging": "discharging_capacity_ah",
            "cumulative": "cumulative_capacity_ah",
            "net": "net_capacity_ah",
        },
        optional_quantity_keys,
    )
    _derive_energy_series(
        series,
        "",
        None,
        {
            "charging": "charging_energy_wh",
            "discharging": "discharging_energy_wh",
            "cumulative": "cumulative_energy_wh",
            "net": "net_energy_wh",
        },
        optional_quantity_keys,
    )

    step_reset_flags = _segment_reset_flags(series, ("step_count", "step_id"), "step_time_second")
    if step_reset_flags is not None:
        _derive_capacity_series(
            series,
            "step",
            step_reset_flags,
            {
                "charging": "step_charging_capacity_ah",
                "discharging": "step_discharging_capacity_ah",
                "cumulative": "step_cumulative_capacity_ah",
                "net": "step_net_capacity_ah",
            },
            optional_quantity_keys,
        )
        _derive_energy_series(
            series,
            "step",
            step_reset_flags,
            {
                "charging": "step_charging_energy_wh",
                "discharging": "step_discharging_energy_wh",
                "cumulative": "step_cumulative_energy_wh",
                "net": "step_net_energy_wh",
            },
            optional_quantity_keys,
        )

    cycle_reset_flags = _segment_reset_flags(series, ("cycle_count",), None)
    if cycle_reset_flags is not None:
        _derive_capacity_series(
            series,
            "cycle",
            cycle_reset_flags,
            {
                "charging": "cycle_charging_capacity_ah",
                "discharging": "cycle_discharging_capacity_ah",
                "cumulative": "cycle_cumulative_capacity_ah",
                "net": "cycle_net_capacity_ah",
            },
            optional_quantity_keys,
        )
        _derive_energy_series(
            series,
            "cycle",
            cycle_reset_flags,
            {
                "charging": "cycle_charging_energy_wh",
                "discharging": "cycle_discharging_energy_wh",
                "cumulative": "cycle_cumulative_energy_wh",
                "net": "cycle_net_energy_wh",
            },
            optional_quantity_keys,
        )


def _derive_capacity_series(
    series: dict[str, list],
    scope: str,
    reset_flags: list[bool] | None,
    keys: dict[str, str],
    optional_quantity_keys: set[str] | None,
):
    del scope
    if not _has_keys(series, "test_time_second", "current_ampere"):
        return

    current = series["current_ampere"]
    derived_values = {
        "charging": _cumulative_integral(series["test_time_second"], [max(value, 0.0) for value in current], reset_flags),
        "discharging": _cumulative_integral(series["test_time_second"], [max(-value, 0.0) for value in current], reset_flags),
        "cumulative": _cumulative_integral(series["test_time_second"], [abs(value) for value in current], reset_flags),
        "net": _cumulative_integral(series["test_time_second"], current, reset_flags),
    }

    for value_key, column_key in keys.items():
        if _quantity_requested(column_key, series, optional_quantity_keys):
            series[column_key] = derived_values[value_key]


def _derive_energy_series(
    series: dict[str, list],
    scope: str,
    reset_flags: list[bool] | None,
    keys: dict[str, str],
    optional_quantity_keys: set[str] | None,
):
    del scope
    if not _has_keys(series, "test_time_second", "current_ampere", "voltage_volt"):
        return

    current = series["current_ampere"]
    power = _power_values(series)
    derived_values = {
        "charging": _cumulative_integral(
            series["test_time_second"],
            [value if current_value > 0 else 0.0 for value, current_value in zip(power, current)],
            reset_flags,
        ),
        "discharging": _cumulative_integral(
            series["test_time_second"],
            [-value if current_value < 0 else 0.0 for value, current_value in zip(power, current)],
            reset_flags,
        ),
        "cumulative": _cumulative_integral(
            series["test_time_second"],
            [value * _sign(current_value) for value, current_value in zip(power, current)],
            reset_flags,
        ),
        "net": _cumulative_integral(series["test_time_second"], power, reset_flags),
    }

    for value_key, column_key in keys.items():
        if _quantity_requested(column_key, series, optional_quantity_keys):
            series[column_key] = derived_values[value_key]


def _cumulative_integral(times: list[float], values: list[float], reset_flags: list[bool] | None = None) -> list[float]:
    result = [0.0] * len(times)
    running_total = 0.0

    for index in range(1, len(times)):
        if reset_flags is not None and reset_flags[index]:
            running_total = 0.0
            result[index] = running_total
            continue

        delta_time = times[index] - times[index - 1]
        if delta_time < 0:
            raise BdfExportError("Cannot derive cumulative BDF quantities from decreasing test time.")

        running_total += 0.5 * (values[index - 1] + values[index]) * delta_time / 3600.0
        result[index] = running_total

    return result


def _segment_reset_flags(series: dict[str, list], segment_keys: tuple[str, ...], reset_time_key: str | None) -> list[bool] | None:
    length = len(series["test_time_second"])
    reset_flags = [False] * length
    reset_flags[0] = True
    has_segment_source = False

    for segment_key in segment_keys:
        if segment_key not in series:
            continue
        has_segment_source = True
        values = series[segment_key]
        for index in range(1, length):
            if values[index] != values[index - 1]:
                reset_flags[index] = True

    if reset_time_key is not None and reset_time_key in series:
        has_segment_source = True
        values = series[reset_time_key]
        for index in range(1, length):
            if values[index] < values[index - 1] or (values[index] == 0 and values[index - 1] != 0):
                reset_flags[index] = True

    if not has_segment_source:
        return None
    return reset_flags


def _quantity_requested(column_key: str, series: dict[str, list], optional_quantity_keys: set[str] | None) -> bool:
    if column_key in series:
        return False
    if column_key in _REQUIRED_BDF_KEYS:
        return True
    return optional_quantity_keys is None or column_key in optional_quantity_keys


def _has_keys(series: dict[str, list], *column_keys: str) -> bool:
    return all(column_key in series for column_key in column_keys)


def _power_values(series: dict[str, list]) -> list[float]:
    if "power_watt" in series:
        return series["power_watt"]
    return [voltage * current for voltage, current in zip(series["voltage_volt"], series["current_ampere"])]


def _sign(value: float) -> float:
    if value > 0:
        return 1.0
    if value < 0:
        return -1.0
    return 0.0


def _detect_bdf_column(base_key: str, array_name: str, array_type: str, quantity: str, unit: str) -> str | None:
    texts = (
        _normalize_text(base_key),
        _normalize_text(array_name),
        _normalize_text(array_type),
        _normalize_text(quantity),
    )
    unit_key = _normalize_unit(unit)
    raw_unit = str(unit).strip()

    # This is another voltage array, so identify it before generic aliases such
    # as "potential" or the voltage-unit fallback can claim it.
    if any(identifier in text for text in texts for identifier in _WEVSCE_IDENTIFIERS):
        return "voltage_wevsce_volt"

    for text in texts:
        column_key = _BDF_ALIAS_TO_KEY.get(text)
        if column_key is not None:
            return column_key

    for term in _BDF_TERMS:
        aliases = tuple(_normalize_text(alias) for alias in term.aliases)
        if any(len(alias) >= 3 and alias in text for text in texts for alias in aliases):
            return term.key

    if unit_key in _TIME_FACTORS and raw_unit != "S":
        return "test_time_second"
    if unit_key in _VOLTAGE_FACTORS:
        return "voltage_volt"
    if unit_key in _CURRENT_FACTORS:
        return "current_ampere"

    return None


def _convert_values(column_key: str, unit: str, values, text_values: tuple) -> list:
    term = _BDF_TERMS_BY_KEY[column_key]
    if term.value_type == "string":
        return [_to_string(value) for value in values]

    factor = _conversion_factor(column_key, unit, text_values)
    return [_to_float(value) * factor for value in values]


def _conversion_factor(column_key: str, unit: str, text_values: tuple = ()) -> float:
    unit_key = _normalize_unit(unit)
    term = _BDF_TERMS_BY_KEY.get(column_key)
    if term is None:
        raise BdfExportError(f"Unsupported BDF column: {column_key}")

    if term.unit_factors is None:
        return 1.0

    factor = term.unit_factors.get(unit_key)
    if factor is not None:
        return factor

    inferred_unit_key = _infer_unit_from_text(term.unit_factors, text_values)
    if inferred_unit_key is not None:
        return term.unit_factors[inferred_unit_key]

    if unit_key == "":
        canonical_unit_key = _canonical_unit_key(column_key)
        if canonical_unit_key in term.unit_factors:
            return term.unit_factors[canonical_unit_key]

    raise BdfExportError(f"Unsupported unit for BDF column {term.label}: {unit!r}")


def _infer_unit_from_text(unit_factors: dict[str, float], text_values: tuple) -> str | None:
    unit_tokens = set()
    for value in text_values:
        unit_tokens.update(_unit_tokens(value))

    normalized_texts = tuple(_normalize_text(value) for value in text_values)
    for unit_key in sorted(unit_factors, key=len, reverse=True):
        if not unit_key:
            continue
        normalized_unit = _normalize_unit(unit_key)
        if normalized_unit in unit_tokens:
            return unit_key

    for unit_key in sorted(unit_factors, key=len, reverse=True):
        if not unit_key:
            continue
        normalized_unit = _normalize_text(unit_key)
        if any(normalized_unit and text.endswith(normalized_unit) for text in normalized_texts):
            return unit_key
    return None


def _unit_tokens(value) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(
        _normalize_unit(token)
        for token in re.split(r"[\s,()/\\\[\];:]+", str(value))
        if token
    )


def _canonical_unit_key(column_key: str) -> str:
    if column_key.endswith("_ohm"):
        return "ohm"
    if column_key.endswith("_pa"):
        return "pa"
    if column_key.endswith("_celsius"):
        return "degc"
    if column_key.endswith("_ah"):
        return "ah"
    if column_key.endswith("_wh"):
        return "wh"
    if column_key.endswith("_hertz"):
        return "hz"
    if column_key.endswith("_degree"):
        return "deg"
    if column_key.endswith("_watt"):
        return "w"
    if column_key.endswith("_ampere"):
        return "a"
    if column_key.endswith("_volt"):
        return "v"
    if column_key.endswith("_second"):
        return "s"
    if column_key in {"cycle_count", "record_index", "step_count", "step_id", "step_index"}:
        return ""
    return ""


def _build_dataframe(series: dict[str, list]) -> pd.DataFrame:
    dataframe_columns = {}
    for term in _BDF_TERMS:
        if term.key in series:
            dataframe_columns[term.label] = series[term.key]

    return pd.DataFrame(dataframe_columns)


def _sort_bdf_dataframe(dataframe: pd.DataFrame) -> pd.DataFrame:
    test_time_label = _BDF_TERMS_BY_KEY["test_time_second"].label
    if test_time_label not in dataframe.columns:
        return dataframe
    return dataframe.sort_values(test_time_label, kind="mergesort", ignore_index=True)


def _write_csv(path: Path, dataframe: pd.DataFrame):
    dataframe.to_csv(path, index=False, float_format="%.15g")


def _write_parquet(path: Path, dataframe: pd.DataFrame):
    dataframe.to_parquet(path, index=False)


def _write_dataframe(path: Path, dataframe: pd.DataFrame, export_type: str):
    if export_type == "csv":
        _write_csv(path, dataframe)
    elif export_type == "parquet":
        _write_parquet(path, dataframe)
    else:
        raise BdfExportError(f"Unsupported BDF export type: {export_type}")


def _unique_output_path(output_dir: Path, stem: str, export_type: str) -> Path:
    normalized_stem = _without_bdf_suffix(stem)
    candidate = output_dir / _bdf_filename(normalized_stem, export_type)
    suffix = 2
    while candidate.exists():
        candidate = output_dir / _bdf_filename(f"{normalized_stem}_{suffix}", export_type)
        suffix += 1
    return candidate


def _bdf_filename(stem: str, export_type: str) -> str:
    return f"{stem}.bdf.{export_type}"


def _without_bdf_suffix(stem: str) -> str:
    return stem[:-4] if stem.casefold().endswith(".bdf") else stem


def _normalize_unit(unit) -> str:
    if unit is None:
        return ""

    normalized = str(unit).strip().casefold()
    normalized = normalized.replace(" ", "")
    normalized = normalized.replace("\N{GREEK SMALL LETTER MU}", "u")
    normalized = normalized.replace("\N{MICRO SIGN}", "u")
    normalized = normalized.replace("\u00ce\u00bc", "u")
    normalized = normalized.replace("\u00c2\u00b5", "u")
    normalized = normalized.replace("\N{DEGREE SIGN}", "deg")
    normalized = normalized.replace("\u00c2\u00b0", "deg")
    normalized = normalized.replace("\N{GREEK CAPITAL LETTER OMEGA}", "ohm")
    normalized = normalized.replace("\N{GREEK SMALL LETTER OMEGA}", "ohm")
    normalized = normalized.replace("deg.c", "degc")
    normalized = normalized.replace("degcelsius", "degc")
    normalized = normalized.replace("degreecelsius", "degc")
    return normalized


def _to_float(value) -> float:
    if hasattr(value, "item"):
        value = value.item()
    return float(value)


def _to_string(value) -> str:
    if hasattr(value, "item"):
        value = value.item()
    return "" if value is None else str(value)
