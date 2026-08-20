from dataclasses import replace

from PySide6.QtWidgets import (
    QWidget,
    QLabel,
    QVBoxLayout,
    QHBoxLayout,
    QToolBar,
    QDialog,
    QFormLayout,
    QDialogButtonBox,
    QMessageBox,
    QFrame,
    QMenu,
    QSizePolicy,
    QToolButton,
)
from PySide6.QtCore import Signal, Qt, QSize
from PySide6.QtGui import QAction
import pyqtgraph as pg
import numpy as np

from src.measurement_data import (
    DatasetView,
    LiveMeasurementStarted,
    LogicalMeasurementRun,
    MeasurementSegment,
    dataset_arrays,
    default_axis_indexes,
    measurement_arrays,
    measurement_dataset_views,
)
from src.widgets import NoScrollComboBox

def _is_metadata_array_name(name):
    return str(name) in {"segment_index", "step_id", "execution_index", "step_type"}

def _get_unit(data_array, default = None):
    return getattr(data_array, "unit", default)

def _get_name(data_array, default = None):
    return getattr(data_array, "name", default)

class _LiveDataset:
    def __init__(self, title, arrays):
        self.title = title
        self._arrays = tuple(arrays)

    def arrays(self):
        return self._arrays


class _LiveMeasurement:
    def __init__(self, title, dataset):
        self.title = title
        self.dataset = dataset


class graph_widget(QWidget):
    dataset_views_changed = Signal()

    HOVER_LABEL_OFFSET = 12
    HOVER_LABEL_MARGIN = 4
    HOVER_MARKER_SIZE = 10
    EIS_SERIES_COLORS = (
        "#2f6f9f",
        "#7c3aed",
        "#d97706",
        "#059669",
        "#dc2626",
        "#0891b2",
    )

    def __init__(self):
        super().__init__()
        self.setObjectName("graphWidget")
        self.measurement = None
        self.x_index = None
        self.y_index = None
        self.right_y_index = None
        self.dataset_view_id = None
        self.nyquist_mode = False
        self.live_dataset_views = []
        self.live_arrays = {}
        self.live_axis_selection = None
        self.live_run: LogicalMeasurementRun | None = None
        self.live_active_segment: MeasurementSegment | None = None
        self.live_current_view: DatasetView | None = None
        self.live_curve = None
        self.primary_curves = []
        self.right_view = None
        self.right_curve = None
        self.snap_hover_to_data = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground("w")
        self.plot_widget.showGrid(x=True, y=True, alpha=0.25)
        self.plot_widget.getAxis("bottom").setPen("#7b8794")
        self.plot_widget.getAxis("left").setPen("#7b8794")
        self.plot_widget.getAxis("bottom").setTextPen("#56616f")
        self.plot_widget.getAxis("left").setTextPen("#56616f")
        self.plot_item = self.plot_widget.getPlotItem()
        self.legend = self.plot_item.addLegend(
            offset=(-10, 10),
            brush=pg.mkBrush(255, 255, 255, 225),
            pen=pg.mkPen("#c7d0da"),
        )
        self.legend.hide()
        self.set_rectangle_zoom_enabled(False)
        self._setup_right_axis()
        self._setup_hover_coordinates()
        layout.addWidget(self.plot_widget)

    def set_rectangle_zoom_enabled(self, enabled: bool):
        mouse_mode = pg.ViewBox.RectMode if enabled else pg.ViewBox.PanMode
        self.plot_item.vb.setMouseMode(mouse_mode)

    def _setup_hover_coordinates(self):
        self.hover_marker = pg.ScatterPlotItem(
            size=self.HOVER_MARKER_SIZE,
            pen=pg.mkPen("#ffffff", width=2),
            brush=pg.mkBrush("#2f6f9f"),
        )
        self._add_hover_marker()
        self.hover_marker.hide()

        self.hover_label = QLabel(self.plot_widget)
        self.hover_label.setObjectName("graphHoverCoordinates")
        self.hover_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hover_label.hide()
        self.plot_item.scene().sigMouseMoved.connect(self._update_hover_coordinates)

    def _add_hover_marker(self):
        self.plot_item.addItem(self.hover_marker, ignoreBounds=True)

    def _update_hover_coordinates(self, scene_position):
        plot_bounds = self.plot_item.vb.sceneBoundingRect()
        if not self.primary_curves or not plot_bounds.contains(scene_position):
            self._hide_hover_coordinates()
            return

        cursor_coordinates = self.plot_item.vb.mapSceneToView(scene_position)
        coordinates = (cursor_coordinates.x(), cursor_coordinates.y())
        if self.snap_hover_to_data:
            coordinates = self._nearest_visible_data_point(*coordinates)
            if coordinates is None:
                self._hide_hover_coordinates()
                return
            self.hover_marker.setData([coordinates[0]], [coordinates[1]])
            self.hover_marker.show()
        else:
            self.hover_marker.hide()

        x_value, y_value = coordinates
        self.hover_label.setText(
            f"x: {x_value:.6g}\n"
            f"y: {y_value:.6g}"
        )
        self.hover_label.adjustSize()

        cursor_position = self.plot_widget.mapFromScene(scene_position)
        label_x = min(
            cursor_position.x() + self.HOVER_LABEL_OFFSET,
            self.plot_widget.width() - self.hover_label.width() - self.HOVER_LABEL_MARGIN,
        )
        label_y = min(
            cursor_position.y() + self.HOVER_LABEL_OFFSET,
            self.plot_widget.height() - self.hover_label.height() - self.HOVER_LABEL_MARGIN,
        )
        self.hover_label.move(
            max(self.HOVER_LABEL_MARGIN, label_x),
            max(self.HOVER_LABEL_MARGIN, label_y),
        )
        self.hover_label.show()

    def _nearest_visible_data_point(self, cursor_x, cursor_y):
        x_range, y_range = self.plot_item.vb.viewRange()
        x_span = abs(x_range[1] - x_range[0]) or 1
        y_span = abs(y_range[1] - y_range[0]) or 1
        plot_bounds = self.plot_item.vb.sceneBoundingRect()
        nearest = None

        for curve in self.primary_curves:
            x_data, y_data = curve.getData()
            if x_data is None or y_data is None:
                continue

            x_data = np.asarray(x_data)
            y_data = np.asarray(y_data)
            visible = (
                np.isfinite(x_data)
                & np.isfinite(y_data)
                & (x_data >= min(x_range))
                & (x_data <= max(x_range))
                & (y_data >= min(y_range))
                & (y_data <= max(y_range))
            )
            if not visible.any():
                continue

            visible_indexes = np.flatnonzero(visible)
            x_distance = (x_data[visible] - cursor_x) * plot_bounds.width() / x_span
            y_distance = (y_data[visible] - cursor_y) * plot_bounds.height() / y_span
            distances = x_distance ** 2 + y_distance ** 2
            local_index = int(np.argmin(distances))
            candidate = (
                float(distances[local_index]),
                float(x_data[visible_indexes[local_index]]),
                float(y_data[visible_indexes[local_index]]),
            )
            if nearest is None or candidate[0] < nearest[0]:
                nearest = candidate

        return None if nearest is None else nearest[1:]

    def set_snap_hover_to_data(self, enabled):
        self.snap_hover_to_data = enabled
        self._hide_hover_coordinates()

    def _hide_hover_coordinates(self):
        self.hover_label.hide()
        self.hover_marker.hide()

    def leaveEvent(self, event):
        self._hide_hover_coordinates()
        super().leaveEvent(event)

    def plot_measurement(self, measurement, selection=None):
        self.measurement = measurement
        self._clear_live_state()
        self.dataset_views_changed.emit()
        if self.nyquist_mode and self.plot_nyquist():
            return
        dataset_view = self._dataset_view_for_selection(measurement, selection)
        self._plot_dataset_view(dataset_view, selection)

    def begin_live_measurement(self, event: LiveMeasurementStarted | None = None):
        event = event or LiveMeasurementStarted()
        self.measurement = None
        if event.segment is None:
            self._clear_live_state()
            self.dataset_view_id = None
        else:
            if self.live_run is None:
                self._clear_live_state()
                self.live_run = LogicalMeasurementRun(event.run_title or "Live run")
                self.dataset_view_id = None
            self.live_active_segment = event.segment
            self.live_arrays = {}
            self.live_current_view = None
            self._refresh_live_dataset_views()

    def complete_live_segment(self, segment: MeasurementSegment):
        if self.live_run is None:
            return

        self.live_run.add_segment(segment)
        self.live_active_segment = None
        self.live_current_view = None
        self._refresh_live_dataset_views()
        self._plot_selected_live_view()

    def _clear_live_state(self):
        self.live_dataset_views = []
        self.live_arrays = {}
        self.live_axis_selection = None
        self.live_run = None
        self.live_active_segment = None
        self.live_current_view = None

    def _plot_dataset_view(self, dataset_view, selection=None):
        arrays = dataset_arrays(dataset_view.dataset) if dataset_view is not None else []

        if not arrays:
            self._prepare_plot()
            return

        if dataset_view is not None:
            self.dataset_view_id = dataset_view.id

        if selection:
            self.x_index = selection["x"]
            self.y_index = selection["left_y"]
            self.right_y_index = selection["right_y"]
            if selection["right_y"] is not None:
                self._plot_dual_arrays_from_indexes(arrays, self.x_index, self.y_index, selection["right_y"])
                return
        else:
            self.x_index, self.y_index = default_axis_indexes(arrays)
            self.right_y_index = None

        if self.x_index >= len(arrays):
            self.x_index = 0
        if self.y_index >= len(arrays):
            self.y_index = min(1, len(arrays) - 1)

        x_array = arrays[self.x_index]
        y_array = arrays[self.y_index]
        if dataset_view is not None and dataset_view.id == "measurement":
            series = self._split_step_series(arrays, self.x_index, self.y_index)
            if len(series) > 1:
                self._plot_labeled_series(
                    series,
                    f"{x_array.name}, {x_array.unit}",
                    f"{y_array.name}, {y_array.unit}",
                )
                return
        if dataset_view is not None and dataset_view.id == "eis":
            series = self._split_eis_series(arrays, self.x_index, self.y_index)
            if len(series) > 1:
                self._plot_labeled_series(
                    series,
                    f"{x_array.name}, {x_array.unit}",
                    f"{y_array.name}, {y_array.unit}",
                )
                return
        self._plot_arrays(x_array.to_numpy(),
                          y_array.to_numpy(),
                          f"{x_array.name}, {x_array.unit}",
                          f"{y_array.name}, {y_array.unit}"
                          )

    @staticmethod
    def _split_step_series(arrays, x_index, y_index):
        metadata = {
            str(_get_name(array, "")): np.asarray(array.to_numpy()).ravel()
            for array in arrays
            if _is_metadata_array_name(_get_name(array, ""))
        }
        segment_indexes = metadata.get("segment_index")
        if segment_indexes is None:
            return []

        x_values = np.asarray(arrays[x_index].to_numpy()).ravel()
        y_values = np.asarray(arrays[y_index].to_numpy()).ravel()
        if x_values.shape != y_values.shape or x_values.shape != segment_indexes.shape:
            return []

        series = []
        start = 0
        while start < len(segment_indexes):
            segment_value = segment_indexes[start]
            end = start + 1
            while end < len(segment_indexes) and segment_indexes[end] == segment_value:
                end += 1
            finite = np.isfinite(x_values[start:end]) & np.isfinite(y_values[start:end])
            if finite.any():
                label = graph_widget._step_series_label(metadata, start, end, len(series) + 1)
                series.append((x_values[start:end][finite], y_values[start:end][finite], label))
            start = end
        return series

    @staticmethod
    def _step_series_label(metadata, start, end, series_number):
        execution_indexes = metadata.get("execution_index", ())[start:end]
        step_types = metadata.get("step_type", ())[start:end]
        execution_index = next(
            (value for value in execution_indexes if graph_widget._has_value(value)),
            series_number,
        )
        step_type = next(
            (str(value) for value in step_types if graph_widget._has_value(value)),
            "step",
        )
        try:
            execution_label = f"{float(execution_index):g}"
        except (TypeError, ValueError):
            execution_label = str(execution_index)
        return f"{execution_label} · {step_type.replace('_', ' ')}"

    @staticmethod
    def _split_eis_series(arrays, x_index, y_index):
        x_values = np.asarray(arrays[x_index].to_numpy()).ravel()
        y_values = np.asarray(arrays[y_index].to_numpy()).ravel()
        return graph_widget._split_eis_values(arrays, x_values, y_values)

    @staticmethod
    def _split_eis_values(arrays, x_values, y_values):
        if x_values.shape != y_values.shape:
            return []

        finite = np.isfinite(x_values) & np.isfinite(y_values)
        starts = np.flatnonzero(finite & np.concatenate(([True], ~finite[:-1])))
        ends = np.flatnonzero(finite & np.concatenate((~finite[1:], [True]))) + 1
        metadata = {
            str(_get_name(array, "")): np.asarray(array.to_numpy()).ravel()
            for array in arrays
            if _is_metadata_array_name(_get_name(array, ""))
        }

        series = []
        for series_number, (start, end) in enumerate(zip(starts, ends), start=1):
            label = graph_widget._eis_series_label(
                metadata,
                int(start),
                int(end),
                series_number,
            )
            series.append((x_values[start:end], y_values[start:end], label))
        return series

    @staticmethod
    def _eis_series_label(metadata, start, end, series_number):
        step_ids = metadata.get("step_id", ())[start:end]
        step_types = metadata.get("step_type", ())[start:end]
        step_id = next((value for value in step_ids if graph_widget._has_value(value)), None)
        step_type = next(
            (str(value) for value in step_types if graph_widget._has_value(value)),
            "",
        )

        details = []
        if step_id is not None:
            numeric_step_id = float(step_id)
            step_label = (
                str(int(numeric_step_id))
                if numeric_step_id.is_integer()
                else f"{numeric_step_id:g}"
            )
            details.append(f"Step {step_label}")
        if step_type:
            details.append(step_type.replace("_", " "))
        suffix = f" — {' · '.join(details)}" if details else ""
        return f"Spectrum {series_number}{suffix}"

    @staticmethod
    def _has_value(value):
        if value is None or str(value) == "":
            return False
        try:
            return bool(np.isfinite(value))
        except TypeError:
            return True

    def _dataset_view_for_selection(self, measurement, selection=None):
        return self._dataset_view_from_views(
            measurement_dataset_views(measurement, include_individual_measurements=True),
            selection,
        )

    def available_dataset_views(self):
        if self.measurement is not None:
            return measurement_dataset_views(
                self.measurement,
                include_individual_measurements=True,
            )
        return list(self.live_dataset_views)

    def select_dataset_view(self, dataset_id: str):
        self.nyquist_mode = False
        target_view = next(
            (view for view in self.available_dataset_views() if view.id == dataset_id),
            None,
        )
        target_arrays = dataset_arrays(target_view.dataset) if target_view is not None else []
        x_index, y_index = default_axis_indexes(target_arrays)
        selection = {
            "dataset_id": dataset_id,
            "x": x_index,
            "left_y": y_index,
            "right_y": None,
        }
        if self.measurement is not None:
            self.plot_measurement(self.measurement, selection=selection)
        else:
            self.plot_live_selection(selection)

    def plot_nyquist(self) -> bool:
        views = self.available_dataset_views()
        unified_view = next((view for view in views if view.id == "eis"), None)
        current_live_view = next((view for view in views if view.id == "eis_live"), None)
        if unified_view is not None:
            eis_views = [unified_view]
            if current_live_view is not None:
                eis_views.append(current_live_view)
        else:
            eis_views = [
                view
                for view in views
                if view.is_eis and "subscan" not in view.id
            ]

        series = []
        primary_view = None
        primary_indexes = None
        for view in eis_views:
            arrays = dataset_arrays(view.dataset)
            components = self._impedance_components(arrays)
            if components is None:
                continue
            real_values, imaginary_values, real_label, imaginary_label, real_index, imaginary_index = components
            if primary_view is None:
                primary_view = view
                primary_indexes = (real_index, imaginary_index)
                primary_labels = (real_label, imaginary_label)
            view_series = self._split_eis_values(arrays, real_values, imaginary_values)
            if not view_series:
                finite = np.isfinite(real_values) & np.isfinite(imaginary_values)
                if finite.any():
                    view_series = [(real_values[finite], imaginary_values[finite], view.title)]
            for x_values, imaginary_values, label in view_series:
                if view.id == "eis_live":
                    label = "Current · EIS"
                series.append((x_values, -np.asarray(imaginary_values), label))

        if not series or primary_view is None or primary_indexes is None:
            return False

        self.nyquist_mode = True
        self.dataset_view_id = primary_view.id
        self.x_index, self.y_index = primary_indexes
        self.right_y_index = None
        self._plot_labeled_series(
            series,
            primary_labels[0],
            f"-{primary_labels[1]}",
        )
        return True

    def has_nyquist_data(self) -> bool:
        for view in self.available_dataset_views():
            if not view.is_eis:
                continue
            arrays = dataset_arrays(view.dataset)
            if self._impedance_components(arrays) is not None:
                return True
        return False

    @classmethod
    def _impedance_components(cls, arrays):
        real_index = cls._find_impedance_array(arrays, imaginary=False)
        imaginary_index = cls._find_impedance_array(arrays, imaginary=True)
        if real_index is not None and imaginary_index is not None:
            real_array = arrays[real_index]
            imaginary_array = arrays[imaginary_index]
            return (
                np.asarray(real_array.to_numpy()).ravel(),
                np.asarray(imaginary_array.to_numpy()).ravel(),
                f"{real_array.name}, {real_array.unit}",
                f"{imaginary_array.name}, {imaginary_array.unit}",
                real_index,
                imaginary_index,
            )

        magnitude_index = cls._find_array_by_identifiers(
            arrays,
            {"z", "impedance", "absoluteimpedance", "impedancemodulus"},
        )
        phase_index = cls._find_array_by_identifiers(
            arrays,
            {"phase", "phaseangle"},
        )
        if magnitude_index is None or phase_index is None:
            return None

        magnitude_array = arrays[magnitude_index]
        phase_array = arrays[phase_index]
        magnitude = np.asarray(magnitude_array.to_numpy(), dtype=float).ravel()
        phase = np.asarray(phase_array.to_numpy(), dtype=float).ravel()
        if magnitude.shape != phase.shape:
            return None
        phase_unit = "".join(
            character
            for character in str(_get_unit(phase_array, "")).casefold()
            if character.isalnum()
        )
        phase_radians = phase if phase_unit in {"rad", "radian", "radians"} else np.deg2rad(phase)
        impedance_unit = _get_unit(magnitude_array, "")
        return (
            magnitude * np.cos(phase_radians),
            magnitude * np.sin(phase_radians),
            f"ZReal, {impedance_unit}",
            f"ZImag, {impedance_unit}",
            magnitude_index,
            phase_index,
        )

    @staticmethod
    def _find_impedance_array(arrays, *, imaginary: bool):
        identifiers = (
            {"zimag", "zim", "zimaginary", "imaginaryimpedance"}
            if imaginary
            else {"zreal", "zre", "realimpedance"}
        )
        for index, data_array in enumerate(arrays):
            texts = (
                _get_name(data_array, ""),
                getattr(data_array, "type", ""),
                getattr(data_array, "quantity", ""),
            )
            normalized = {
                "".join(character for character in str(text).casefold() if character.isalnum())
                for text in texts
            }
            if normalized.intersection(identifiers):
                return index
        return None

    @staticmethod
    def _find_array_by_identifiers(arrays, identifiers):
        for index, data_array in enumerate(arrays):
            texts = (
                _get_name(data_array, ""),
                getattr(data_array, "type", ""),
                getattr(data_array, "quantity", ""),
            )
            normalized = {
                "".join(character for character in str(text).casefold() if character.isalnum())
                for text in texts
            }
            if normalized.intersection(identifiers):
                return index
        return None

    def _dataset_view_from_views(self, dataset_views, selection=None):
        if not dataset_views:
            return None

        selected_dataset_id = selection.get("dataset_id") if selection else None
        if selected_dataset_id is not None:
            for dataset_view in dataset_views:
                if dataset_view.id == selected_dataset_id:
                    return dataset_view

        if self.dataset_view_id is not None:
            for dataset_view in dataset_views:
                if dataset_view.id == self.dataset_view_id:
                    return dataset_view

        for dataset_view in dataset_views:
            if not dataset_view.is_eis:
                return dataset_view
        return dataset_views[0]

    def plot_live_data(self, callback_data):
        self.measurement = None
        dataset_view = self._live_dataset_view(callback_data)
        if dataset_view is None:
            return

        self.live_current_view = dataset_view
        self._refresh_live_dataset_views()
        self._plot_selected_live_view()

    def _plot_selected_live_view(self):
        if self.nyquist_mode and self.plot_nyquist():
            return
        selection = self.live_axis_selection
        dataset_view = self._dataset_view_from_views(self.live_dataset_views, selection)
        if dataset_view is None:
            return
        if selection and selection.get("dataset_id") != dataset_view.id:
            selection = None
            self.live_axis_selection = None
        self._plot_dataset_view(dataset_view, selection)

    def plot_live_selection(self, selection):
        self.live_axis_selection = selection
        dataset_view = self._dataset_view_from_views(self.live_dataset_views, selection)
        self._plot_dataset_view(dataset_view, selection)

    def _refresh_live_dataset_views(self):
        views = self._accumulated_live_dataset_views()
        if self.live_current_view is not None:
            views.append(self.live_current_view)
        self.live_dataset_views = views
        self.dataset_views_changed.emit()

    def _accumulated_live_dataset_views(self):
        if self.live_run is None:
            return []

        segments = list(self.live_run.segments)
        if (
            self.live_active_segment is not None
            and self.live_current_view is not None
            and not self.live_current_view.is_eis
        ):
            live_source = _LiveMeasurement(
                self.live_active_segment.label,
                self.live_current_view.dataset,
            )
            segments.append(replace(self.live_active_segment, source=live_source))

        live_run = LogicalMeasurementRun(
            f"{self.live_run.title} (live)",
            segments,
        )
        views = measurement_dataset_views(
            live_run,
            include_individual_measurements=True,
            include_individual_eis=False,
        )
        titles = {
            "measurement": "Entire run (live)",
            "eis": "Entire run EIS (live)",
        }
        return [replace(view, title=titles.get(view.id, view.title)) for view in views]

    def _live_dataset_view(self, callback_data):
        x_array = getattr(callback_data, "x_array", None)
        y_array = getattr(callback_data, "y_array", None)
        if x_array is not None and y_array is not None:
            for data_array in (x_array, y_array):
                self.live_arrays[self._live_array_key(data_array)] = data_array
            dataset = _LiveDataset("Current step (live)", tuple(self.live_arrays.values()))
            return DatasetView("live", "Current step (live)", dataset, callback_data)

        dataset = getattr(callback_data, "data", None)
        if dataset is None:
            return None
        return DatasetView(
            "eis_live",
            "Current step EIS (live)",
            dataset,
            callback_data,
            is_eis=True,
        )

    @staticmethod
    def _live_array_key(data_array):
        name = str(_get_name(data_array, "") or "")
        if name:
            return ("name", name)
        return (
            "metadata",
            str(getattr(data_array, "type", "") or ""),
            str(getattr(data_array, "quantity", "") or ""),
            str(_get_unit(data_array, "") or ""),
        )

    def _plot_arrays(self, x_array, y_array, x_label, y_label):
        x_array = np.asarray(x_array).ravel()
        y_array = np.asarray(y_array).ravel()
        if x_array.shape != y_array.shape:
            return
        self._prepare_plot()
        self.plot_widget.setLabel("bottom", f"{x_label}")
        self.plot_widget.setLabel("left", f"{y_label}")
        pen = pg.mkPen(color="#2f6f9f", width=2)
        self.live_curve = self.plot_widget.plot(
            x_array,
            y_array,
            pen=pen,
            connect="finite",
        )
        self.primary_curves = [self.live_curve]
        self._add_hover_marker()

    def _plot_labeled_series(self, series, x_label, y_label):
        self._prepare_plot()
        self.plot_widget.setLabel("bottom", f"{x_label}")
        self.plot_widget.setLabel("left", f"{y_label}")

        for index, (x_values, y_values, label) in enumerate(series):
            color = self.EIS_SERIES_COLORS[index % len(self.EIS_SERIES_COLORS)]
            curve = self.plot_item.plot(
                x_values,
                y_values,
                pen=pg.mkPen(color=color, width=2),
                connect="finite",
                name=label,
            )
            self.primary_curves.append(curve)

        self.live_curve = self.primary_curves[0] if self.primary_curves else None
        self.legend.show()
        self._add_hover_marker()

    def _prepare_plot(self):
        self._hide_hover_coordinates()
        self.plot_widget.clear()
        self._clear_right_axis()
        self.legend.clear()
        self.legend.hide()
        self.live_curve = None
        self.primary_curves = []

    def _plot_dual_arrays_from_indexes(self, arrays, x_index, left_y_index, right_y_index):
        if x_index >= len(arrays):
            x_index = 0
        if left_y_index >= len(arrays):
            left_y_index = min(1, len(arrays) - 1)
        if right_y_index >= len(arrays):
            right_y_index = None

        x_array = arrays[x_index]
        left_y_array = arrays[left_y_index]
        if right_y_index is None:
            self._plot_arrays(
                x_array.to_numpy(),
                left_y_array.to_numpy(),
                f"{x_array.name}, {x_array.unit}",
                f"{left_y_array.name}, {left_y_array.unit}",
            )
            return

        right_y_array = arrays[right_y_index]
        self._plot_dual_arrays(
            x_array.to_numpy(),
            left_y_array.to_numpy(),
            x_array.to_numpy(),
            right_y_array.to_numpy(),
            f"{x_array.name}, {x_array.unit}",
            f"{left_y_array.name}, {left_y_array.unit}",
            f"{right_y_array.name}, {right_y_array.unit}",
        )

    def _plot_dual_arrays(self, left_x, left_y, right_x, right_y, x_label, left_label, right_label):
        left_x = np.asarray(left_x).ravel()
        left_y = np.asarray(left_y).ravel()
        right_x = np.asarray(right_x).ravel()
        right_y = np.asarray(right_y).ravel()
        if left_x.shape != left_y.shape:
            return
        if right_x.shape != right_y.shape:
            self._plot_arrays(left_x, left_y, x_label, left_label)
            return

        self._prepare_plot()
        self.plot_item.showAxis("right")
        self.plot_item.setLabel("bottom", f"{x_label}")
        self.plot_item.setLabel("left", f"{left_label}", color="#2f6f9f")
        self.plot_item.setLabel("right", f"{right_label}", color="#7c3aed")

        self.live_curve = self.plot_item.plot(
            left_x,
            left_y,
            pen=pg.mkPen(color="#2f6f9f", width=2),
            connect="finite",
        )
        self.primary_curves = [self.live_curve]
        self.right_curve = pg.PlotDataItem(
            right_x,
            right_y,
            pen=pg.mkPen(color="#7c3aed", width=2),
            connect="finite",
        )
        self.right_view.addItem(self.right_curve)
        self._update_right_axis()
        self.right_view.autoRange()
        self._add_hover_marker()

    def _setup_right_axis(self):
        self.right_view = pg.ViewBox()
        self.plot_item.showAxis("right")
        self.plot_item.scene().addItem(self.right_view)
        self.plot_item.getAxis("right").linkToView(self.right_view)
        self.right_view.setXLink(self.plot_item.vb)
        self.plot_item.getAxis("right").setPen("#7b8794")
        self.plot_item.getAxis("right").setTextPen("#56616f")
        self.plot_item.vb.sigResized.connect(self._update_right_axis)
        self.plot_item.hideAxis("right")

    def _clear_right_axis(self):
        if self.right_view is not None:
            self.right_view.clear()
        self.right_curve = None
        self.plot_item.hideAxis("right")

    def _update_right_axis(self): # TODO:fixa till eis data, inte numrerat? eller pröva fler eis? 
        if self.right_view is None:
            return
        self.right_view.setGeometry(self.plot_item.vb.sceneBoundingRect())
        self.right_view.linkedViewChanged(self.plot_item.vb, self.right_view.XAxis)
    
class axis_selection_dialog(QDialog):
    def __init__(
        self,
        dataset_views,
        current_dataset_id=None,
        current_x=0,
        current_y=1,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Edit Axes")

        layout = QFormLayout(self)
        self.dataset_combo = NoScrollComboBox(self)
        self.x_combo = NoScrollComboBox(self)
        self.left_y_combo = NoScrollComboBox(self)
        self.right_y_combo = NoScrollComboBox(self)
        self.dataset_views = dataset_views
        self.arrays = []
        self.current_x = current_x
        self.current_y = current_y
        self.current_dataset_id = current_dataset_id

        self.rebuild_dataset_choice()
        self.rebuild_axis_choice() 
        
        self.dataset_combo.currentIndexChanged.connect(self.rebuild_axis_choice)

        layout.addRow("Dataset", self.dataset_combo)
        layout.addRow("X axis", self.x_combo)
        layout.addRow("Left Y axis", self.left_y_combo)
        layout.addRow("Right Y axis", self.right_y_combo)
        

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def rebuild_dataset_choice(self):
        self.dataset_combo.clear()
        for dataset_view in self.dataset_views:
            self.dataset_combo.addItem(dataset_view.title, dataset_view.id)

        if self.current_dataset_id is not None:
            self._set_combo_to_data(self.dataset_combo, self.current_dataset_id)
        elif self.dataset_combo.count():
            self.dataset_combo.setCurrentIndex(0)

    def rebuild_axis_choice(self):
        self.x_combo.clear()
        self.left_y_combo.clear()
        self.right_y_combo.clear()
        dataset_view = self.selected_dataset_view()
        self.arrays = dataset_arrays(dataset_view.dataset) if dataset_view is not None else []

        self.right_y_combo.addItem("None", None)
        for index, data_array in enumerate(self.arrays):
            if _is_metadata_array_name(_get_name(data_array, "")):
                continue
            label = self._format_array_label(index, data_array)
            self.x_combo.addItem(label, index)
            self.left_y_combo.addItem(label, index)
            self.right_y_combo.addItem(label, index)
        if self.dataset_combo.currentData() == self.current_dataset_id:
            x_index = self.current_x
            y_index = self.current_y
        else:
            x_index, y_index = default_axis_indexes(self.arrays)
        self._set_combo_to_data(self.x_combo, x_index)
        self._set_combo_to_data(self.left_y_combo, y_index)
        
    def selected_axes(self):
        return {
            "dataset_id": self.dataset_combo.currentData(),
            "x": self.x_combo.currentData(),
            "left_y": self.left_y_combo.currentData(),
            "right_y": self.right_y_combo.currentData(),
        }

    def selected_dataset_view(self):
        dataset_id = self.dataset_combo.currentData()
        for dataset_view in self.dataset_views:
            if dataset_view.id == dataset_id:
                return dataset_view
        return self.dataset_views[0] if self.dataset_views else None

    @staticmethod
    def _set_combo_to_data(combo, value):
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)
        elif combo.count():
            combo.setCurrentIndex(0)

    @staticmethod
    def _format_array_label(index, data_array):
        name = _get_name(data_array, f"array_{index}")
        unit = _get_unit(data_array, "")
        array_type = getattr(data_array, "type", "")

        details = [detail for detail in (array_type, unit) if detail]
        if details:
            return f"{index}: {name} ({', '.join(details)})"
        return f"{index}: {name}"

class graph_panel(QFrame):
    run_requested = Signal()
    edit_requested = Signal()
    stop_requested = Signal()
    expand_requested = Signal(bool)
    selection_requested = Signal()

    def __init__(self, title, instrument=None):
        super().__init__()
        self.setObjectName("graphPanel")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.instrument = instrument
        self.base_title = title

        self.graph = graph_widget()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)

        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)

        self.title_label = QLabel(self.base_title)
        self.title_label.setObjectName("graphPanelTitle")
        header_layout.addWidget(self.title_label, 1)

        self.toolbar = QToolBar("Graph Utilities", self)
        self.toolbar.setObjectName("graphToolbar")
        self.toolbar.setMovable(False)
        self.toolbar.setIconSize(QSize(16, 16))
        self.toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        header_layout.addWidget(self.toolbar, 0, Qt.AlignmentFlag.AlignRight)

        layout.addLayout(header_layout)
        self.data_controls_widget = QWidget(self)
        self.data_controls_widget.setObjectName("expandedDataControls")
        data_controls_layout = QHBoxLayout(self.data_controls_widget)
        data_controls_layout.setContentsMargins(0, 0, 0, 0)
        data_controls_layout.setSpacing(8)
        data_controls_layout.addWidget(QLabel("View", self.data_controls_widget))
        self.step_view_combo = NoScrollComboBox(self.data_controls_widget)
        self.step_view_combo.setMinimumWidth(220)
        data_controls_layout.addWidget(self.step_view_combo, 1)
        self.nyquist_button = QToolButton(self.data_controls_widget)
        self.nyquist_button.setObjectName("nyquistButton")
        self.nyquist_button.setText("Nyquist")
        self.nyquist_button.setCheckable(True)
        self.nyquist_button.setToolTip("Plot all EIS spectra as -ZImag versus ZReal")
        data_controls_layout.addWidget(self.nyquist_button)
        self.data_controls_widget.setVisible(False)
        layout.addWidget(self.data_controls_widget)
        layout.addWidget(self.graph, 1)

        self.run_action = QAction("Run", self)
        self.edit_action = QAction("Edit", self)
        self.stop_action = QAction("Stop", self)
        self.expand_action = QAction("Expand", self)
        self.expand_action.setCheckable(True)
        self.zoom_area_action = QAction("Zoom Area", self)
        self.zoom_area_action.setCheckable(True)
        self.zoom_area_action.setToolTip("Select an area to zoom; turn off to pan")
        self.axes_action = QAction("Edit Axes", self)
        self.highlight_points_action = QAction("Highlight Points", self)
        self.highlight_points_action.setCheckable(True)
        self.highlight_points_action.setToolTip(
            "Snap hover values to the nearest visible point on the primary curve"
        )

        self.view_menu = QMenu(self)
        self.view_menu.addAction(self.zoom_area_action)
        self.view_menu.addAction(self.highlight_points_action)
        self.view_action = QAction("View", self)
        self.view_action.setMenu(self.view_menu)

        self.toolbar.addAction(self.run_action)
        self.toolbar.addAction(self.edit_action)
        self.toolbar.addAction(self.stop_action)
        self.toolbar.addAction(self.expand_action)
        self.toolbar.addAction(self.axes_action)
        self.toolbar.addAction(self.view_action)
        view_button = self.toolbar.widgetForAction(self.view_action)
        if isinstance(view_button, QToolButton):
            view_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.run_button = self.toolbar.widgetForAction(self.run_action)
        self.edit_button = self.toolbar.widgetForAction(self.edit_action)
        self.stop_button = self.toolbar.widgetForAction(self.stop_action)
        if isinstance(self.run_button, QToolButton):
            self.run_button.setObjectName("channelRunButton")
        if isinstance(self.edit_button, QToolButton):
            self.edit_button.setObjectName("channelEditButton")
        if isinstance(self.stop_button, QToolButton):
            self.stop_button.setObjectName("channelStopButton")
        self.toolbar.actionTriggered.connect(lambda _action: self.selection_requested.emit())
        self.view_menu.triggered.connect(lambda _action: self.selection_requested.emit())
        self.graph.plot_item.scene().sigMouseClicked.connect(
            lambda _event: self.selection_requested.emit()
        )

        overflow_button = self.toolbar.findChild(QToolButton, "qt_toolbar_ext_button")
        if overflow_button is not None:
            overflow_button.setArrowType(Qt.ArrowType.NoArrow)
            overflow_button.setText("...")
            overflow_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            overflow_button.setToolTip("More graph actions")

        self.run_action.triggered.connect(self.run_requested.emit)
        self.edit_action.triggered.connect(self.edit_requested.emit)
        self.stop_action.triggered.connect(self.stop_requested.emit)
        self.expand_action.toggled.connect(self.expand_requested.emit)
        self.zoom_area_action.toggled.connect(self.graph.set_rectangle_zoom_enabled)
        self.axes_action.triggered.connect(self.edit_axes)
        self.highlight_points_action.toggled.connect(self.graph.set_snap_hover_to_data)
        self.step_view_combo.currentIndexChanged.connect(self.change_step_view)
        self.nyquist_button.toggled.connect(self.set_nyquist_view)
        self.graph.dataset_views_changed.connect(self.refresh_data_controls)
        self._is_running = False
        self._is_configured = False
        self.set_running(False)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.selection_requested.emit()
        super().mousePressEvent(event)

    def set_selected(self, is_selected: bool):
        self.setProperty("selected", is_selected)
        self.style().unpolish(self)
        self.style().polish(self)

    def set_running(self, is_running: bool):
        self._is_running = is_running
        self.run_action.setEnabled(self._is_configured and not is_running)
        self.edit_action.setEnabled(not is_running)
        self.stop_action.setEnabled(is_running)

    def set_configured(self, is_configured: bool):
        self._is_configured = is_configured
        self.run_action.setEnabled(is_configured and not self._is_running)
        for button in (self.run_button, self.edit_button):
            if not isinstance(button, QToolButton):
                continue
            button.setProperty("configured", is_configured)
            button.style().unpolish(button)
            button.style().polish(button)

    def set_status_text(self, status: str | None = None):
        if status:
            self.title_label.setText(f"{self.base_title} [{status}]")
        else:
            self.title_label.setText(self.base_title)

    def set_expanded(self, is_expanded: bool):
        self.expand_action.blockSignals(True)
        self.expand_action.setChecked(is_expanded)
        self.expand_action.setText("Restore" if is_expanded else "Expand")
        self.expand_action.blockSignals(False)
        self.data_controls_widget.setVisible(is_expanded)
        if is_expanded:
            self.refresh_data_controls()

    def refresh_data_controls(self):
        views = self.graph.available_dataset_views()
        selectable_views = [
            view
            for view in views
            if not view.is_eis
            or (
                view.id != "eis"
                and "subscan" not in view.id
            )
        ]
        previous_id = self.step_view_combo.currentData()

        self.step_view_combo.blockSignals(True)
        self.step_view_combo.clear()
        for view in selectable_views:
            if view.id == "measurement":
                label = "All steps"
            elif view.id == "live":
                label = "Current · live"
            elif view.id == "eis_live":
                label = "Current · EIS"
            elif view.id.startswith("eis_step_"):
                parts = view.id.split("_")
                execution_index = parts[2] if len(parts) > 2 else "?"
                spectrum_number = parts[3] if len(parts) > 3 else "1"
                suffix = f" {spectrum_number}" if spectrum_number != "1" else ""
                label = f"{execution_index} · EIS{suffix}"
            else:
                label = view.title
            self.step_view_combo.addItem(label, view.id)

        preferred_id = previous_id
        available_ids = {view.id for view in selectable_views}
        if preferred_id not in available_ids and self.graph.dataset_view_id in available_ids:
            preferred_id = self.graph.dataset_view_id
        if preferred_id not in available_ids and "measurement" in available_ids:
            preferred_id = "measurement"
        preferred_index = self.step_view_combo.findData(preferred_id)
        if preferred_index >= 0:
            self.step_view_combo.setCurrentIndex(preferred_index)
        self.step_view_combo.blockSignals(False)

        has_nyquist = self.graph.has_nyquist_data()
        self.nyquist_button.setEnabled(has_nyquist)
        self.nyquist_button.blockSignals(True)
        self.nyquist_button.setChecked(self.graph.nyquist_mode and has_nyquist)
        self.nyquist_button.blockSignals(False)
        self.step_view_combo.setEnabled(bool(selectable_views) and not self.nyquist_button.isChecked())

    def change_step_view(self):
        dataset_id = self.step_view_combo.currentData()
        if dataset_id is None:
            return
        self.nyquist_button.blockSignals(True)
        self.nyquist_button.setChecked(False)
        self.nyquist_button.blockSignals(False)
        self.graph.select_dataset_view(dataset_id)

    def set_nyquist_view(self, enabled: bool):
        if enabled:
            if not self.graph.plot_nyquist():
                self.nyquist_button.blockSignals(True)
                self.nyquist_button.setChecked(False)
                self.nyquist_button.blockSignals(False)
        else:
            self.graph.nyquist_mode = False
            dataset_id = self.step_view_combo.currentData()
            if dataset_id is not None:
                self.graph.select_dataset_view(dataset_id)
        self.refresh_data_controls()

    def edit_axes(self):
        measurement = self.graph.measurement
        if measurement is None:
            dataset_views = self.graph.live_dataset_views
        else:
            dataset_views = measurement_dataset_views(
                measurement,
                include_individual_measurements=True,
            )

        if not dataset_views:
            QMessageBox.information(
                self,
                "No data loaded",
                "Load a measurement or wait for live data before editing axes.",
            )
            return

        if measurement is None:
            current_dataset_view = self.graph._dataset_view_from_views(dataset_views)
            arrays = dataset_arrays(current_dataset_view.dataset) if current_dataset_view is not None else []
        else:
            current_dataset_view = self.graph._dataset_view_for_selection(measurement)
            arrays = dataset_arrays(current_dataset_view.dataset) if current_dataset_view is not None else measurement_arrays(measurement)

        if not arrays:
            QMessageBox.warning(
                self,
                "No data arrays",
                "The current data does not contain any plottable arrays.",
            )
            return

        current_x = self.graph.x_index if isinstance(self.graph.x_index, int) else 0
        current_y = self.graph.y_index if isinstance(self.graph.y_index, int) else min(1, len(arrays) - 1)

        dialog = axis_selection_dialog(
            dataset_views,
            current_dataset_id=(
                self.graph.dataset_view_id
                or (current_dataset_view.id if current_dataset_view is not None else None)
            ),
            current_x=current_x,
            current_y=current_y,
            parent=self,
        )
        if dialog.exec():
            selection = dialog.selected_axes()
            self.nyquist_button.blockSignals(True)
            self.nyquist_button.setChecked(False)
            self.nyquist_button.blockSignals(False)
            self.graph.nyquist_mode = False
            if measurement is None:
                self.graph.plot_live_selection(selection)
            else:
                self.graph.plot_measurement(measurement, selection=selection)
