import sys
from datetime import date
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pypalmsens as ps
from src.app_style import APP_STYLESHEET
from aurora_method_builder.methods import (
    AURORA_ADDITIONAL_MEASUREMENT_DESCRIPTIONS,
    AURORA_ADDITIONAL_MEASUREMENT_OPTIONS,
    AURORA_DEVICE_MEASUREMENT_TYPES,
    AURORA_DEVICE_OPTIONS,
    AuroraExportSettings,
    build_aurora_stepwise_method,
    load_aurora_package,
)

from src.bdf_export import BdfExportError, bdf_optional_quantity_choices, export_measurement_to_bdf_files
from src.channel_status import channel_status_snapshot
from PySide6.QtCore import QObject, QSize, Signal, Qt, QProcess
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from src.graph import graph_panel
from src.measurement_data import (
    AuroraStepCompleted,
    LiveMeasurementStarted,
    LogicalMeasurementRun,
)
from src.method_config import (
    CURRENT_RANGE_FIELD_KEYS,
    CURRENT_RANGE_OPTIONS,
    METHOD_ORDER,
    METHOD_SPECS,
    build_method,
)
from src.palmsens_service import palmsens_connection_service
from src.temperature_chamber.temperature_controller import TemperatureProgress, TemperatureSettings
from src.widgets import NoScrollComboBox
import src.device_helpers as pslib

PANEL_COLUMNS = 3


def _sanitize_filename_component(value: str) -> str:
    cleaned = "".join(character if character.isalnum() else "_" for character in value.strip())
    return cleaned.strip("_")

def _abbreviate_filename_component(value: str) -> str:
    cleaned = _sanitize_filename_component(value or "step")
    short = "".join(part[0].upper() for part in cleaned.split("_") if part)
    return short

def _default_bdf_export_stem(cell_name: str, cas_id: str, sequence_number: int) -> str:
    sanitized_cell_name = _sanitize_filename_component(cell_name)
    sanitized_cas_id = _sanitize_filename_component(cas_id)
    export_date = date.today().strftime("%Y%m%d")
    if sanitized_cas_id:
        return f"UU_{sanitized_cell_name}_{sanitized_cas_id}_{export_date}_{sequence_number:04d}"
    return f"UU_{sanitized_cell_name}_{export_date}_{sequence_number:04d}"


def _custom_bdf_export_stem(
    base_name: str,
    measurement_number: int | str,
    step_type: str | None,
    include_step_type: bool,
) -> str:
    parts = [
        _sanitize_filename_component(base_name) or "measurement",
        _sanitize_filename_component(str(measurement_number)) or "x",
    ]
    if include_step_type:
        parts.append(_abbreviate_filename_component(step_type or "step") or "step")
    return "_".join(parts)


@dataclass(frozen=True)
class BdfAutoSaveSettings:
    output_dir: Path
    export_type: str
    cell_name: str
    cas_id: str
    optional_quantity_keys: set[str]
    custom_naming_enabled: bool = False
    custom_base_name: str = ""
    include_step_type: bool = False


class connection_indicator(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("connectionIndicator")
        self.set_status(False)

    # Enheten är egentligen inte ansluten tills mätningar, kanske ändra?
    def set_status(self, is_connected: bool, dev: pslib.discovered_device | None = None):
        if is_connected and dev is not None:
            self.setText(f"Connected to {dev.name}")
            self.setStyleSheet("color: green;")
            return

        self.setText("Disconnected")
        self.setStyleSheet("color: red;")


class device_selection_dialog(QDialog):
    def __init__(self, devices, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select Device")

        layout = QVBoxLayout(self)
        self.device_list = list_choices()
        self.device_list.set_choice(devices)
        layout.addWidget(self.device_list)

        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self.select_device)
        layout.addWidget(self.connect_button)

        self.selected_device = None

    def select_device(self):
        dev = self.device_list.get_selected_choice()
        if dev is None:
            return

        self.selected_device = dev
        self.accept()


class bdf_export_dialog(QDialog):
    def __init__(self, exportable_panels, parent=None):
        super().__init__(parent)
        self.file_type = "csv"
        self.setWindowTitle("Export BDF")
        self.resize(640, 620)
        self._checkboxes: list[tuple[QCheckBox, object]] = []
        self._quantity_checkboxes: list[tuple[QCheckBox, str]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(10)

        info_label = QLabel(
            "Select the channel measurements to export as BDF files. "
            "Only channels with loaded measurements are listed.",
            self,
        )
        info_label.setWordWrap(True)
        layout.addWidget(info_label)

        self.output_dir_edit = QLineEdit(self)
        browse_button = QPushButton("Choose Folder", self)
        browse_button.clicked.connect(self.choose_output_dir)

        self.file_type_combo_box = NoScrollComboBox(self)
        self.file_type_combo_box.addItem("csv", "csv")
        self.file_type_combo_box.addItem("parquet", "parquet")

        self.cell_name_edit = QLineEdit(self)
        self.cell_name_edit.setPlaceholderText("e.g. A0001")

        self.cas_id_edit = QLineEdit(self)
        self.cas_id_edit.setPlaceholderText("e.g. nisu1374")

        output_options = QWidget(self)
        output_options_layout = QGridLayout(output_options)
        output_options_layout.setContentsMargins(0, 0, 0, 0)
        output_options_layout.setHorizontalSpacing(8)
        output_options_layout.setVerticalSpacing(8)
        output_options_layout.addWidget(QLabel("Format", output_options), 0, 0)
        output_options_layout.addWidget(self.file_type_combo_box, 0, 1, 1, 2)
        output_options_layout.setColumnStretch(1, 1)
        layout.addWidget(output_options)

        naming_header = QLabel("Naming", self)
        layout.addWidget(naming_header)

        naming_frame = QFrame(self)
        naming_frame.setFrameShape(QFrame.Shape.StyledPanel)
        naming_frame.setFrameShadow(QFrame.Shadow.Plain)
        naming_layout = QGridLayout(naming_frame)
        naming_layout.setContentsMargins(10, 10, 10, 10)
        naming_layout.setHorizontalSpacing(8)
        naming_layout.setVerticalSpacing(8)
        naming_layout.addWidget(QLabel("Cell name", naming_frame), 0, 0)
        naming_layout.addWidget(self.cell_name_edit, 0, 1)
        naming_layout.addWidget(QLabel("CAS ID", naming_frame), 1, 0)
        naming_layout.addWidget(self.cas_id_edit, 1, 1)
        naming_layout.setColumnStretch(1, 1)
        layout.addWidget(naming_frame)

        folder_options = QWidget(self)
        folder_options_layout = QGridLayout(folder_options)
        folder_options_layout.setContentsMargins(0, 0, 0, 0)
        folder_options_layout.setHorizontalSpacing(8)
        folder_options_layout.setVerticalSpacing(8)
        folder_options_layout.addWidget(QLabel("Folder", folder_options), 0, 0)
        folder_options_layout.addWidget(self.output_dir_edit, 0, 1)
        folder_options_layout.addWidget(browse_button, 0, 2)
        folder_options_layout.setColumnStretch(1, 1)
        layout.addWidget(folder_options)

        channel_header = QLabel("Channels", self)
        layout.addWidget(channel_header)

        self.checkbox_container = QWidget(self)
        self.checkbox_layout = QVBoxLayout(self.checkbox_container)
        self.checkbox_layout.setContentsMargins(0, 0, 0, 0)
        self.checkbox_layout.setSpacing(6)

        for panel in exportable_panels:
            checkbox = QCheckBox(panel.base_title, self.checkbox_container)
            checkbox.setChecked(False)
            self._checkboxes.append((checkbox, panel))
            self.checkbox_layout.addWidget(checkbox)

        self.checkbox_layout.addStretch(1)

        channel_scroll_area = QScrollArea(self)
        channel_scroll_area.setWidgetResizable(True)
        channel_scroll_area.setMinimumHeight(72)
        channel_scroll_area.setMaximumHeight(132)
        channel_scroll_area.setWidget(self.checkbox_container)
        layout.addWidget(channel_scroll_area)

        self.quantity_toggle_button = QToolButton(self)
        self.quantity_toggle_button.setCheckable(True)
        self.quantity_toggle_button.setChecked(False)
        self.quantity_toggle_button.setArrowType(Qt.ArrowType.RightArrow)
        self.quantity_toggle_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.quantity_toggle_button.toggled.connect(self.set_quantity_options_visible)
        layout.addWidget(self.quantity_toggle_button)

        self.quantity_options_widget = QWidget(self)
        quantity_options_layout = QVBoxLayout(self.quantity_options_widget)
        quantity_options_layout.setContentsMargins(0, 0, 0, 0)
        quantity_options_layout.setSpacing(8)

        self.quantity_search_edit = QLineEdit(self)
        self.quantity_search_edit.setPlaceholderText("Search optional quantities")
        self.quantity_search_edit.setClearButtonEnabled(True)
        self.quantity_search_edit.textChanged.connect(self.filter_optional_quantities)

        quantity_actions_layout = QHBoxLayout()
        quantity_actions_layout.setContentsMargins(0, 0, 0, 0)
        quantity_actions_layout.setSpacing(8)

        select_all_button = QPushButton("Select All", self)
        select_all_button.clicked.connect(self.select_all_optional_quantities)
        clear_button = QPushButton("Clear", self)
        clear_button.clicked.connect(self.clear_optional_quantities)
        quantity_actions_layout.addWidget(self.quantity_search_edit, 1)
        quantity_actions_layout.addWidget(select_all_button)
        quantity_actions_layout.addWidget(clear_button)
        quantity_options_layout.addLayout(quantity_actions_layout)

        self.quantity_container = QWidget(self)
        self.quantity_layout = QVBoxLayout(self.quantity_container)
        self.quantity_layout.setContentsMargins(0, 0, 0, 0)
        self.quantity_layout.setSpacing(4)

        for quantity_key, quantity_label in bdf_optional_quantity_choices():
            checkbox = QCheckBox(quantity_label, self.quantity_container)
            checkbox.setChecked(True)
            checkbox.stateChanged.connect(self.update_quantity_toggle_text)
            self._quantity_checkboxes.append((checkbox, quantity_key))
            self.quantity_layout.addWidget(checkbox)

        self.no_quantity_matches_label = QLabel("No matching quantities", self.quantity_container)
        self.no_quantity_matches_label.setVisible(False)
        self.quantity_layout.addWidget(self.no_quantity_matches_label)
        self.quantity_layout.addStretch(1)

        quantity_scroll_area = QScrollArea(self)
        quantity_scroll_area.setWidgetResizable(True)
        quantity_scroll_area.setMinimumHeight(100)
        quantity_scroll_area.setMaximumHeight(160)
        quantity_scroll_area.setWidget(self.quantity_container)
        quantity_options_layout.addWidget(quantity_scroll_area, 1)
        self.quantity_options_widget.setVisible(False)
        layout.addWidget(self.quantity_options_widget)
        self.update_quantity_toggle_text()

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        export_button = button_box.button(QDialogButtonBox.StandardButton.Ok)
        if export_button is not None:
            export_button.setText("Export")
        button_box.accepted.connect(self.validate_and_accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def choose_output_dir(self):
        directory = QFileDialog.getExistingDirectory(self, "Choose export folder")
        if directory:
            self.output_dir_edit.setText(directory)
    
    def selected_panels(self):
        return [panel for checkbox, panel in self._checkboxes if checkbox.isChecked()]

    def selected_type(self):
        return self.file_type_combo_box.currentData()

    def cell_name(self):
        return self.cell_name_edit.text().strip() or "A0001"

    def cas_id(self):
        return self.cas_id_edit.text().strip()

    def selected_optional_quantity_keys(self):
        return {
            quantity_key
            for checkbox, quantity_key in self._quantity_checkboxes
            if checkbox.isChecked()
        }

    def filter_optional_quantities(self, search_text: str):
        normalized_search = search_text.strip().casefold()
        visible_count = 0
        for checkbox, quantity_key in self._quantity_checkboxes:
            searchable_text = f"{checkbox.text()} {quantity_key}".casefold()
            is_visible = not normalized_search or normalized_search in searchable_text
            checkbox.setVisible(is_visible)
            if is_visible:
                visible_count += 1
        self.no_quantity_matches_label.setVisible(visible_count == 0)

    def set_quantity_options_visible(self, is_visible: bool):
        self.quantity_options_widget.setVisible(is_visible)
        arrow_type = Qt.ArrowType.DownArrow if is_visible else Qt.ArrowType.RightArrow
        self.quantity_toggle_button.setArrowType(arrow_type)

    def select_all_optional_quantities(self):
        for checkbox, _ in self._quantity_checkboxes:
            checkbox.setChecked(True)
        self.update_quantity_toggle_text()

    def clear_optional_quantities(self):
        for checkbox, _ in self._quantity_checkboxes:
            checkbox.setChecked(False)
        self.update_quantity_toggle_text()

    def update_quantity_toggle_text(self):
        selected_count = len(self.selected_optional_quantity_keys())
        total_count = len(self._quantity_checkboxes)
        if selected_count == total_count:
            summary = "all selected"
        else:
            summary = f"{selected_count} selected"
        self.quantity_toggle_button.setText(f"Additional BDF quantities ({summary})")

    def output_directory(self) -> Path | None:
        raw_path = self.output_dir_edit.text().strip()
        if not raw_path:
            return None
        return Path(raw_path)

    def validate_and_accept(self):
        output_dir = self.output_directory()
        if output_dir is None:
            QMessageBox.warning(self, "Export error", "Choose an export folder.")
            return

        if not self.selected_panels():
            QMessageBox.warning(self, "Export error", "Select at least one channel to export.")
            return

        self.accept()


class method_configuration_dialog(QDialog):
    def __init__(
        self,
        title: str,
        instrument=None,
        current_range_options: dict[str, tuple[str, ...]] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setObjectName("methodConfigDialog")
        self.setWindowTitle(f"Run Measurement - {title}")
        self.resize(760, 620)
        self.setMinimumSize(560, 420)
        self.dialog_title = title
        self.method = None
        self.method_label = ""
        self.temperature_settings = None
        self.bdf_auto_save_settings = None
        self.instrument = instrument
        self.current_range_options = current_range_options or {}
        self.imported_package = None
        self.imported_package_path: Path | None = None
        self.field_widgets: dict[str, QWidget] = {}
        self.additional_measurement_checks: dict[str, QCheckBox] = {}

        dialog_layout = QVBoxLayout(self)
        dialog_layout.setContentsMargins(16, 16, 16, 16)
        dialog_layout.setSpacing(12)

        self.scroll_content = QWidget(self)
        layout = QVBoxLayout(self.scroll_content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self.form_layout = QFormLayout()
        self.form_layout.setContentsMargins(0, 0, 0, 0)
        self.form_layout.setHorizontalSpacing(12)
        self.form_layout.setVerticalSpacing(8)
        layout.addLayout(self.form_layout)

        self.run_mode_combo = NoScrollComboBox(self)
        self.run_mode_combo.addItem("PalmSens method", "native")
        self.run_mode_combo.addItem("Imported package", "aurora_package")
        self.run_mode_combo.addItem("MethodScript", "methodscript")
        self.form_layout.addRow("Run type", self.run_mode_combo)

        self.method_combo = NoScrollComboBox(self)
        for method_key in METHOD_ORDER:
            spec = METHOD_SPECS[method_key]
            self.method_combo.addItem(spec.label, method_key)
        self.method_combo_label = QLabel("Method", self)
        self.form_layout.addRow(self.method_combo_label, self.method_combo)

        self.field_form = QFormLayout()
        layout.addLayout(self.field_form)

        self.package_widget = QFrame(self)
        self.package_widget.setObjectName("auroraOptionsCard")
        self.package_widget.setFrameShape(QFrame.Shape.StyledPanel)
        package_layout = QVBoxLayout(self.package_widget)
        package_layout.setContentsMargins(14, 14, 14, 14)
        package_layout.setSpacing(10)

        package_title = QLabel("Imported Package", self.package_widget)
        package_title.setObjectName("auroraCardTitle")
        package_layout.addWidget(package_title)

        self.package_info_label = QLabel("No package loaded.", self.package_widget)
        self.package_info_label.setWordWrap(True)
        package_layout.addWidget(self.package_info_label)

        self.load_package_button = QPushButton("Load Package", self.package_widget)
        self.load_package_button.clicked.connect(self.load_aurora_package_file)
        package_layout.addWidget(self.load_package_button, 0, Qt.AlignmentFlag.AlignLeft)

        self.package_run_form = QFormLayout()
        self.package_run_form.setContentsMargins(0, 0, 0, 0)
        self.package_run_form.setHorizontalSpacing(12)
        self.package_run_form.setVerticalSpacing(8)
        package_layout.addLayout(self.package_run_form)

        def add_package_run_field(label_text: str, widget: QWidget, tooltip: str):
            label = QLabel(label_text, self.package_widget)
            label.setToolTip(tooltip)
            widget.setToolTip(tooltip)
            self.package_run_form.addRow(label, widget)

        self.aurora_sample_name_edit = QLineEdit("", self.package_widget)
        add_package_run_field(
            "Sample name",
            self.aurora_sample_name_edit,
            "Overrides the sample name stored in the imported package. "
            "Leave blank to use the package value.",
        )

        self.aurora_capacity_edit = QLineEdit("", self.package_widget)
        add_package_run_field(
            "Capacity (mAh)",
            self.aurora_capacity_edit,
            "Sample capacity in mAh. It is used to convert C-rate steps and limits "
            "to current. Mandatory if using steps that depend on C-rate",
        )

        self.aurora_device_combo = NoScrollComboBox(self.package_widget)
        for label, value in AURORA_DEVICE_OPTIONS:
            self.aurora_device_combo.addItem(label, value)
        add_package_run_field(
            "PalmSens target",
            self.aurora_device_combo,
            "PalmSens instrument model used to validate the imported package and "
            "generate compatible MethodSCRIPT.",
        )

        self.aurora_scan_step_edit = QLineEdit("", self.package_widget)
        add_package_run_field(
            "Scan step voltage (V)",
            self.aurora_scan_step_edit,
            "Voltage interval that controls the rate at which voltage is sampled "
            "during a voltage sweep. Either set this value or define the voltage "
            "recording delta (record.voltage_V) in the imported package if using a"
            "voltage-scan step",
        )

        self.aurora_eis_dc_potential_edit = QLineEdit("0.0", self.package_widget)
        add_package_run_field(
            "EIS DC potential (V)",
            self.aurora_eis_dc_potential_edit,
            "DC potential offset, in volts, applied during potentiostatic EIS steps.",
        )

        self.aurora_eis_dc_current_edit = QLineEdit("0.0", self.package_widget)
        add_package_run_field(
            "EIS DC current (mA)",
            self.aurora_eis_dc_current_edit,
            "DC current offset, in mA, applied during galvanostatic EIS steps.",
        )

        extra_measurements_label = QLabel("Extra measurements", self.package_widget)
        extra_measurements_label.setObjectName("auroraCardTitle")
        package_layout.addWidget(extra_measurements_label)

        self.additional_measurement_widget = QWidget(self.package_widget)
        self.additional_measurement_layout = QGridLayout(self.additional_measurement_widget)
        self.additional_measurement_layout.setContentsMargins(0, 0, 0, 0)
        self.additional_measurement_layout.setHorizontalSpacing(16)
        self.additional_measurement_layout.setVerticalSpacing(6)
        for index, (var_type, label) in enumerate(AURORA_ADDITIONAL_MEASUREMENT_OPTIONS):
            checkbox = QCheckBox(label, self.additional_measurement_widget)
            description = AURORA_ADDITIONAL_MEASUREMENT_DESCRIPTIONS[var_type]
            checkbox.setToolTip(
                f"{description}\nMethodSCRIPT variable type: {var_type} (added with add_meas)."
            )
            self.additional_measurement_checks[var_type] = checkbox
            self.additional_measurement_layout.addWidget(checkbox, index // 2, index % 2)
        package_layout.addWidget(self.additional_measurement_widget)

        bdf_auto_save_title = QLabel("BDF auto-save", self.package_widget)
        bdf_auto_save_title.setObjectName("auroraCardTitle")
        package_layout.addWidget(bdf_auto_save_title)

        self.aurora_auto_bdf_checkbox = QCheckBox(
            "Save BDF after each measurement step",
            self.package_widget,
        )
        package_layout.addWidget(self.aurora_auto_bdf_checkbox)

        self.aurora_auto_bdf_widget = QWidget(self.package_widget)
        aurora_auto_bdf_layout = QGridLayout(self.aurora_auto_bdf_widget)
        aurora_auto_bdf_layout.setContentsMargins(0, 0, 0, 0)
        aurora_auto_bdf_layout.setHorizontalSpacing(8)
        aurora_auto_bdf_layout.setVerticalSpacing(8)

        default_bdf_dir = Path(__file__).parent.parent / "out2" / "temp"
        self.aurora_auto_bdf_dir_edit = QLineEdit(str(default_bdf_dir), self.aurora_auto_bdf_widget)
        self.aurora_auto_bdf_browse_button = QPushButton("Choose Folder", self.aurora_auto_bdf_widget)
        self.aurora_auto_bdf_browse_button.clicked.connect(self.choose_auto_bdf_output_dir)
        aurora_auto_bdf_layout.addWidget(QLabel("Folder", self.aurora_auto_bdf_widget), 0, 0)
        aurora_auto_bdf_layout.addWidget(self.aurora_auto_bdf_dir_edit, 0, 1)
        aurora_auto_bdf_layout.addWidget(self.aurora_auto_bdf_browse_button, 0, 2)

        self.aurora_auto_bdf_type_combo = NoScrollComboBox(self.aurora_auto_bdf_widget)
        self.aurora_auto_bdf_type_combo.addItem("csv", "csv")
        self.aurora_auto_bdf_type_combo.addItem("parquet", "parquet")
        aurora_auto_bdf_layout.addWidget(QLabel("Format", self.aurora_auto_bdf_widget), 1, 0)
        aurora_auto_bdf_layout.addWidget(self.aurora_auto_bdf_type_combo, 1, 1, 1, 2)

        self.aurora_auto_bdf_cell_name_edit = QLineEdit("A0001", self.aurora_auto_bdf_widget)
        self.aurora_auto_bdf_cell_name_edit.setPlaceholderText("e.g. A0001")
        aurora_auto_bdf_layout.addWidget(QLabel("Cell name", self.aurora_auto_bdf_widget), 2, 0)
        aurora_auto_bdf_layout.addWidget(self.aurora_auto_bdf_cell_name_edit, 2, 1, 1, 2)

        self.aurora_auto_bdf_cas_id_edit = QLineEdit("", self.aurora_auto_bdf_widget)
        self.aurora_auto_bdf_cas_id_edit.setPlaceholderText("e.g. nisu1374")
        aurora_auto_bdf_layout.addWidget(QLabel("CAS ID", self.aurora_auto_bdf_widget), 3, 0)
        aurora_auto_bdf_layout.addWidget(self.aurora_auto_bdf_cas_id_edit, 3, 1, 1, 2)

        self.aurora_custom_naming_widget = QFrame(self.aurora_auto_bdf_widget)
        self.aurora_custom_naming_widget.setObjectName("customNamingCard")
        custom_naming_layout = QVBoxLayout(self.aurora_custom_naming_widget)
        custom_naming_layout.setContentsMargins(10, 8, 10, 8)
        custom_naming_layout.setSpacing(7)

        custom_naming_title = QLabel("Custom naming", self.aurora_custom_naming_widget)
        custom_naming_title.setObjectName("auroraCardTitle")
        custom_naming_layout.addWidget(custom_naming_title)

        self.aurora_custom_naming_checkbox = QCheckBox(
            "Enable custom naming",
            self.aurora_custom_naming_widget,
        )
        self.aurora_custom_naming_checkbox.setToolTip(
            "Replace the standard cell, CAS ID, date, and sequence-based file name "
            "with a custom base name."
        )
        custom_naming_layout.addWidget(self.aurora_custom_naming_checkbox)

        custom_naming_form = QFormLayout()
        custom_naming_form.setContentsMargins(0, 0, 0, 0)
        custom_naming_form.setHorizontalSpacing(8)
        custom_naming_form.setVerticalSpacing(7)

        self.aurora_custom_base_name_edit = QLineEdit("", self.aurora_custom_naming_widget)
        self.aurora_custom_base_name_edit.setPlaceholderText("e.g. experiment_1")
        base_name_label = QLabel("Base name", self.aurora_custom_naming_widget)
        base_name_tooltip = (
            "Text placed at the beginning of every custom BDF file name. "
            "Unsupported filename characters are replaced with underscores."
        )
        base_name_label.setToolTip(base_name_tooltip)
        self.aurora_custom_base_name_edit.setToolTip(base_name_tooltip)
        custom_naming_form.addRow(base_name_label, self.aurora_custom_base_name_edit)

        self.aurora_custom_step_type_checkbox = QCheckBox(
            "Include step type",
            self.aurora_custom_naming_widget,
        )
        self.aurora_custom_step_type_checkbox.setToolTip(
            "Append the package step type, such as constant_current or temperature, "
            "after the measurement number."
        )
        custom_naming_form.addRow("", self.aurora_custom_step_type_checkbox)
        custom_naming_layout.addLayout(custom_naming_form)

        self.aurora_filename_preview_label = QLabel(self.aurora_custom_naming_widget)
        self.aurora_filename_preview_label.setObjectName("filenamePreview")
        self.aurora_filename_preview_label.setWordWrap(True)
        self.aurora_filename_preview_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.aurora_filename_preview_label.setToolTip(
            "Example file name. At run time, x is replaced by the measurement number "
            "and step_type by the actual package step type."
        )
        custom_naming_layout.addWidget(self.aurora_filename_preview_label)

        aurora_auto_bdf_layout.addWidget(self.aurora_custom_naming_widget, 4, 0, 1, 3)
        aurora_auto_bdf_layout.setColumnStretch(1, 1)
        package_layout.addWidget(self.aurora_auto_bdf_widget)

        temperature_title = QLabel("Temperature Chamber", self.package_widget)
        temperature_title.setObjectName("auroraCardTitle")
        package_layout.addWidget(temperature_title)

        self.temperature_enabled_checkbox = QCheckBox("Enable Arduino temperature chamber", self.package_widget)
        self.temperature_enabled_checkbox.setToolTip(
            "Connect to the automatically detected Arduino temperature chamber and "
            "execute temperature steps from the imported package."
        )
        package_layout.addWidget(self.temperature_enabled_checkbox)

        self.temperature_form = QFormLayout()
        self.temperature_form.setContentsMargins(0, 0, 0, 0)
        self.temperature_form.setHorizontalSpacing(12)
        self.temperature_form.setVerticalSpacing(8)
        package_layout.addLayout(self.temperature_form)

        def add_temperature_field(label_text: str, widget: QWidget, tooltip: str):
            label = QLabel(label_text, self.package_widget)
            label.setToolTip(tooltip)
            widget.setToolTip(tooltip)
            self.temperature_form.addRow(label, widget)

        self.temperature_tolerance_edit = QLineEdit("0.5", self.package_widget)
        add_temperature_field(
            "Tolerance (degC)",
            self.temperature_tolerance_edit,
            "Maximum allowed difference between the measured chamber temperature "
            "and the target, in degrees Celsius. For temperature steps that wait "
            "for stability, the hold timer resets whenever the temperature moves "
            "outside this tolerance. This setting has no effect when the package "
            "step is configured to start its timer immediately at step start.",
        )

        default_log_dir = Path(__file__).parent.parent / "out2" / "temp_logs"
        self.temperature_log_dir_edit = QLineEdit(str(default_log_dir), self.package_widget)
        add_temperature_field(
            "Log directory",
            self.temperature_log_dir_edit,
            "Folder for timestamped temperature-chamber status and command logs. "
            "The folder is created automatically; leave blank to disable logging.",
        )

        self.temperature_stop_on_abort_checkbox = QCheckBox("Stop chamber on abort", self.package_widget)
        self.temperature_stop_on_abort_checkbox.setChecked(True)
        self.temperature_stop_on_abort_checkbox.setToolTip(
            "Send a stop command to the temperature chamber when the measurement is "
            "aborted. If unchecked, the chamber connection closes without stopping it."
        )
        package_layout.addWidget(self.temperature_stop_on_abort_checkbox)

        layout.addWidget(self.package_widget)

        self.script_help = QLabel(self)
        self.script_help.setObjectName("auroraHelpText")
        self.script_help.setWordWrap(True)
        layout.addWidget(self.script_help)

        self.script_actions = QWidget(self)
        self.script_actions_layout = QVBoxLayout(self.script_actions)
        self.script_actions_layout.setContentsMargins(0, 0, 0, 0)
        self.script_actions_layout.setSpacing(0)
        self.load_methodscript_button = QPushButton("Load MethodSCRIPT", self.script_actions)
        self.load_methodscript_button.clicked.connect(self.load_methodscript)
        self.script_actions_layout.addWidget(self.load_methodscript_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.script_actions)

        self.script_editor = QPlainTextEdit(self)
        self.script_editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.script_editor.setMinimumHeight(320)
        layout.addWidget(self.script_editor, 1)

        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setWidget(self.scroll_content)
        dialog_layout.addWidget(self.scroll_area, 1)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self.run_button = button_box.button(QDialogButtonBox.StandardButton.Ok)
        if self.run_button is not None:
            self.run_button.setText("Run")
        button_box.accepted.connect(self.validate_and_accept)
        button_box.rejected.connect(self.reject)
        dialog_layout.addWidget(button_box)

        self.method_combo.currentIndexChanged.connect(self.rebuild_fields)
        self.run_mode_combo.currentIndexChanged.connect(self.rebuild_mode)
        self.aurora_device_combo.currentIndexChanged.connect(self.update_additional_measurements)
        self.temperature_enabled_checkbox.toggled.connect(self.update_temperature_fields)
        self.aurora_auto_bdf_checkbox.toggled.connect(self.update_auto_bdf_fields)
        self.aurora_custom_naming_checkbox.toggled.connect(self.update_auto_bdf_fields)
        self.aurora_custom_step_type_checkbox.toggled.connect(self.update_bdf_filename_preview)
        self.aurora_custom_base_name_edit.textChanged.connect(self.update_bdf_filename_preview)
        self.aurora_auto_bdf_cell_name_edit.textChanged.connect(self.update_bdf_filename_preview)
        self.aurora_auto_bdf_cas_id_edit.textChanged.connect(self.update_bdf_filename_preview)
        self.aurora_auto_bdf_type_combo.currentIndexChanged.connect(
            self.update_bdf_filename_preview
        )
        self.update_additional_measurements()
        self.update_temperature_fields()
        self.update_auto_bdf_fields()
        self.rebuild_fields()
        self.rebuild_mode()

    def selected_method_key(self) -> str:
        return self.method_combo.currentData()

    def selected_run_mode(self) -> str:
        return self.run_mode_combo.currentData()

    def raw_params(self) -> dict[str, str]:
        params = {}
        for field_key, widget in self.field_widgets.items():
            if isinstance(widget, QLineEdit):
                params[field_key] = widget.text().strip()
            elif isinstance(widget, NoScrollComboBox):
                params[field_key] = str(widget.currentData())
        return params

    def run_channel(self) -> int:
        # run channel will always be 0 as each channel is its own single channel device
        # if self.instrument is not None and getattr(self.instrument, "channel", -1) > 0:
        #    return self.instrument.channel - 1
        return 0

    def update_additional_measurements(self):
        device_key = self.aurora_device_combo.currentData()
        supported = AURORA_DEVICE_MEASUREMENT_TYPES.get(device_key, set())
        for var_type, checkbox in self.additional_measurement_checks.items():
            enabled = var_type in supported
            checkbox.setEnabled(enabled)
            if not enabled:
                checkbox.setChecked(False)

    def selected_additional_measurements(self) -> tuple[str, ...]:
        return tuple(
            var_type
            for var_type, checkbox in self.additional_measurement_checks.items()
            if checkbox.isEnabled() and checkbox.isChecked()
        )

    def update_temperature_fields(self):
        enabled = self.temperature_enabled_checkbox.isChecked()
        for widget in (
            self.temperature_tolerance_edit,
            self.temperature_log_dir_edit,
            self.temperature_stop_on_abort_checkbox,
        ):
            widget.setEnabled(enabled)

    def choose_auto_bdf_output_dir(self):
        directory = QFileDialog.getExistingDirectory(self, "Choose BDF auto-save folder")
        if directory:
            self.aurora_auto_bdf_dir_edit.setText(directory)

    def update_auto_bdf_fields(self):
        enabled = self.aurora_auto_bdf_checkbox.isChecked()
        self.aurora_auto_bdf_widget.setVisible(enabled)
        custom_naming = self.aurora_custom_naming_checkbox.isChecked()
        self.aurora_custom_base_name_edit.setEnabled(custom_naming)
        self.aurora_custom_step_type_checkbox.setEnabled(custom_naming)
        self.aurora_auto_bdf_cell_name_edit.setEnabled(not custom_naming)
        self.aurora_auto_bdf_cas_id_edit.setEnabled(not custom_naming)
        self.update_bdf_filename_preview()

    def update_bdf_filename_preview(self):
        export_type = self.aurora_auto_bdf_type_combo.currentData() or "csv"
        if self.aurora_custom_naming_checkbox.isChecked():
            base_name = self.aurora_custom_base_name_edit.text().strip() or "base_name"
            filename_stem = _custom_bdf_export_stem(
                base_name,
                "x",
                "step_type",
                self.aurora_custom_step_type_checkbox.isChecked(),
            )
        else:
            filename_stem = _default_bdf_export_stem(
                self.aurora_auto_bdf_cell_name_edit.text().strip() or "A0001",
                self.aurora_auto_bdf_cas_id_edit.text().strip(),
                1,
            )
        self.aurora_filename_preview_label.setText(
            f"Preview: {filename_stem}.bdf.{export_type}"
        )

    def build_bdf_auto_save_settings(self) -> BdfAutoSaveSettings | None:
        if not self.aurora_auto_bdf_checkbox.isChecked():
            return None

        raw_output_dir = self.aurora_auto_bdf_dir_edit.text().strip()
        if not raw_output_dir:
            raise ValueError("BDF auto-save folder is required.")

        custom_naming = self.aurora_custom_naming_checkbox.isChecked()
        custom_base_name = self.aurora_custom_base_name_edit.text().strip()
        if custom_naming and not self._is_valid_filename_base(custom_base_name):
            raise ValueError("A base name containing at least one letter or number is required.")

        return BdfAutoSaveSettings(
            output_dir=Path(raw_output_dir),
            export_type=self.aurora_auto_bdf_type_combo.currentData(),
            cell_name=self.aurora_auto_bdf_cell_name_edit.text().strip() or "A0001",
            cas_id=self.aurora_auto_bdf_cas_id_edit.text().strip(),
            optional_quantity_keys={quantity_key for quantity_key, _ in bdf_optional_quantity_choices()},
            custom_naming_enabled=custom_naming,
            custom_base_name=custom_base_name,
            include_step_type=self.aurora_custom_step_type_checkbox.isChecked(),
        )

    @staticmethod
    def _is_valid_filename_base(value: str) -> bool:
        return any(character.isalnum() for character in value)

    def build_temperature_settings(self) -> TemperatureSettings | None:
        if not self.temperature_enabled_checkbox.isChecked():
            return None

        tolerance_c = self.parse_float(self.temperature_tolerance_edit, "Temperature tolerance")
        if tolerance_c <= 0:
            raise ValueError("Temperature tolerance must be greater than 0.")

        return TemperatureSettings(
            tolerance_c=tolerance_c,
            log_dir=self.temperature_log_dir_edit.text().strip() or None,
            stop_on_abort=self.temperature_stop_on_abort_checkbox.isChecked(),
        )

    def build_aurora_export_settings(self) -> AuroraExportSettings:
        return AuroraExportSettings(
            sample_name=self.aurora_sample_name_edit.text().strip() or None,
            capacity_mAh=self.parse_optional_float(self.aurora_capacity_edit, "Capacity (mAh)"),
            device_key=self.aurora_device_combo.currentData(),
            channel=self.run_channel(),
            scan_step_voltage_v=self.parse_optional_float(
                self.aurora_scan_step_edit,
                "Scan step voltage (V)",
            ),
            eis_dc_potential_v=self.parse_float(
                self.aurora_eis_dc_potential_edit,
                "EIS DC potential (V)",
            ),
            eis_dc_current_ma=self.parse_float(
                self.aurora_eis_dc_current_edit,
                "EIS DC current (mA)",
            ),
            additional_measurements=self.selected_additional_measurements(),
        )

    def rebuild_fields(self):
        while self.field_form.rowCount():
            self.field_form.removeRow(0)

        self.field_widgets.clear()
        spec = METHOD_SPECS[self.selected_method_key()]
        for field in spec.fields:
            if field.key in CURRENT_RANGE_FIELD_KEYS:
                options = self.current_range_options.get(field.key) or CURRENT_RANGE_OPTIONS
                widget = NoScrollComboBox(self)
                for option in options:
                    widget.addItem(option, option)
                default_index = widget.findData(field.default)
                if default_index >= 0:
                    widget.setCurrentIndex(default_index)
                tooltip = (
                    "Select a current range supported by the connected PalmSens instrument. "
                    "The applied or measured current values are expressed relative to this range."
                )
                label = QLabel(field.label, self)
                label.setToolTip(tooltip)
                widget.setToolTip(tooltip)
                self.field_widgets[field.key] = widget
                self.field_form.addRow(label, widget)
                continue

            widget = QLineEdit(field.default, self)
            self.field_widgets[field.key] = widget
            self.field_form.addRow(field.label, widget)

    def rebuild_mode(self):
        run_mode = self.selected_run_mode()
        native_mode = run_mode == "native"
        package_mode = run_mode == "aurora_package"
        methodscript_mode = run_mode == "methodscript"

        self.method_combo_label.setVisible(native_mode)
        self.method_combo.setVisible(native_mode)
        for widget in self.field_widgets.values():
            widget.setVisible(native_mode)
        for row_index in range(self.field_form.rowCount()):
            label_item = self.field_form.itemAt(row_index, QFormLayout.ItemRole.LabelRole)
            field_item = self.field_form.itemAt(row_index, QFormLayout.ItemRole.FieldRole)
            if label_item is not None and label_item.widget() is not None:
                label_item.widget().setVisible(native_mode)
            if field_item is not None and field_item.widget() is not None:
                field_item.widget().setVisible(native_mode)

        self.package_widget.setVisible(package_mode)
        self.script_help.setVisible(package_mode or methodscript_mode)
        self.script_actions.setVisible(methodscript_mode)
        self.script_editor.setVisible(methodscript_mode)

        if package_mode:
            self.script_help.setText(
                "Load a `.psmethod` file exported from the standalone Aurora Method Builder. "
                "The package will be rendered for the current channel panel when you run it."
            )
        elif run_mode == "methodscript":
            self.script_help.setText(
                "Paste MethodSCRIPT directly or load an existing .mscr file, then run it with PyPalmSens."
            )
        else:
            self.script_help.clear()

    def validate_and_accept(self):
        try:
            run_mode = self.selected_run_mode()
            if run_mode == "native":
                self.method = build_method(self.selected_method_key(), self.raw_params())
                self.method_label = METHOD_SPECS[self.selected_method_key()].label
                self.temperature_settings = None
                self.bdf_auto_save_settings = None
            else:
                self.method = self.build_script_method(run_mode)
                self.temperature_settings = (
                    self.build_temperature_settings()
                    if run_mode == "aurora_package"
                    else None
                )
                self.bdf_auto_save_settings = (
                    self.build_bdf_auto_save_settings()
                    if run_mode == "aurora_package"
                    else None
                )
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid parameters", str(exc))
            return
        except RuntimeError as exc:
            QMessageBox.warning(self, "Setup error", str(exc))
            return
        except Exception as exc:
            QMessageBox.warning(self, "Method error", str(exc))
            return

        self.accept()
                
    def build_script_method(self, run_mode: str):
        if run_mode == "methodscript":
            script_text = self.script_editor.toPlainText()
            if not script_text.strip():
                raise ValueError("script content is required.")
            self.method_label = "MethodSCRIPT"
            return ps.MethodScript(script=script_text)

        if run_mode != "aurora_package":
            raise ValueError("Unsupported script mode.")

        if self.imported_package is None:
            raise ValueError("Load an Aurora package before running it.")

        if not hasattr(ps, "MethodScript"):
            raise RuntimeError(
                "This PyPalmSens installation does not expose `MethodScript`. "
                "Update PyPalmSens before running imported  packages."
            )

        self.method_label = f"{self.imported_package.name} (step-wise)"
        return build_aurora_stepwise_method(
            self.imported_package,
            self.build_aurora_export_settings(),
        )

    def load_aurora_package_file(self):
        if self.selected_run_mode() != "aurora_package":
            return

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Load Aurora Package",
            "",
            "Aurora Method Packages (*.psmethod);;JSON Files (*.json);;All Files (*)",
        )
        if not file_path:
            return

        try:
            self.imported_package = load_aurora_package(file_path)
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Load failed",
                f"Could not load Aurora package:\n{exc}",
            )
            return

        self.imported_package_path = Path(file_path)
        self.package_info_label.setText(self.package_summary_text())

    def load_methodscript(self):
        if self.selected_run_mode() != "methodscript":
            return

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Load MethodSCRIPT",
            "",
            "MethodSCRIPT Files (*.mscr);;Text Files (*.txt);;All Files (*)",
        )
        if not file_path:
            return

        path = Path(file_path)
        try:
            script_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(
                self,
                "Load failed",
                f"Could not load MethodSCRIPT:\n{exc}",
            )
            return

        self.script_editor.setPlainText(script_text)

    def package_summary_text(self) -> str:
        if self.imported_package is None:
            return "No Aurora package loaded."

        source_name = self.imported_package_path.name if self.imported_package_path is not None else "Unknown"
        return (
            f"Package: {self.imported_package.name}\n"
            f"Source file: {source_name}"
        )

    @staticmethod
    def parse_float(widget: QLineEdit, label: str) -> float:
        raw_value = widget.text().strip()
        if not raw_value:
            raise ValueError(f"{label} is required.")
        try:
            return float(raw_value)
        except ValueError as exc:
            raise ValueError(f"Invalid value for {label}: {raw_value}") from exc

    @staticmethod
    def parse_optional_float(widget: QLineEdit, label: str) -> float | None:
        raw_value = widget.text().strip()
        if not raw_value:
            return None
        try:
            return float(raw_value)
        except ValueError as exc:
            raise ValueError(f"Invalid value for {label}: {raw_value}") from exc

class list_choices(QWidget):
    def __init__(self):
        super().__init__()

        layout = QVBoxLayout(self)
        self.list_widget = QListWidget(self)
        layout.addWidget(self.list_widget)
        self.choices = []

    def set_choice(self, choices):
        self.choices = choices
        self.list_widget.clear()

        for dev in choices:
            self.list_widget.addItem(str(dev.name))

    def get_selected_choice(self):
        row = self.list_widget.currentRow()
        if 0 <= row < len(self.choices):
            return self.choices[row]
        return None


class device_state(QObject):
    connected = Signal(object)
    disconnected = Signal()
    connection_changed = Signal(bool)

    def __init__(self):
        super().__init__()
        self.is_connected = False
        self.device = None

    def set_connected_device(self, dev: pslib.discovered_device):
        if self.is_connected:
            return

        self.device = dev
        self.is_connected = True
        self.connected.emit(dev)
        self.connection_changed.emit(True)

    def clear_connected_device(self):
        if not self.is_connected or self.device is None:
            return

        self.is_connected = False
        self.device = None
        self.disconnected.emit()
        self.connection_changed.emit(False)


class main_window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.panels: list[graph_panel] = []
        self.expanded_panel: graph_panel | None = None
        self.active_runs: dict[graph_panel, int] = {}
        self.stopping_panels: set[graph_panel] = set()
        self.next_run_id = 1
        self.run_panels: dict[int, graph_panel] = {}
        self.run_method_labels: dict[int, str] = {}
        self.run_bdf_auto_save_settings: dict[int, BdfAutoSaveSettings] = {}
        self.run_bdf_auto_save_sequences: dict[int, set[int]] = {}
        self.run_bdf_auto_save_failed: set[int] = set()
        self.selected_panel: graph_panel | None = None
        self.channel_statuses: dict[graph_panel, channel_status_snapshot] = {}
        self.pending_device = None

        self.setWindowTitle("Palmsens demo")
        self.resize(1200, 760)
        self.setMinimumSize(900, 600)

        self.device_state = device_state()
        self.connection_service = palmsens_connection_service(self)

        toolbar = QToolBar("Main Toolbar")
        toolbar.setObjectName("mainToolbar")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(18, 18))
        self.addToolBar(toolbar)

        self.connect_action = QAction("Connect", self)
        self.connect_action.setStatusTip("Scan for available devices")
        self.connect_action.triggered.connect(self.scan_devices)
        toolbar.addAction(self.connect_action)

        self.disconnect_action = QAction("Disconnect", self)
        self.disconnect_action.setStatusTip("Disconnect from device")
        self.disconnect_action.setEnabled(False)
        self.disconnect_action.triggered.connect(self.request_disconnect)
        toolbar.addAction(self.disconnect_action)

        self.aurora_builder_action = QAction("Aurora Builder", self)
        self.aurora_builder_action.setStatusTip("Open the standalone Aurora method builder")
        self.aurora_builder_action.triggered.connect(self.open_aurora_builder)
        toolbar.addAction(self.aurora_builder_action)

        self.session_menu = QMenu("Session", self)
        self.open_action = QAction("Load session", self)
        self.open_action.triggered.connect(self.open_session)
        self.session_menu.addAction(self.open_action)

        self.save_action = QAction("Save session", self)
        self.save_action.triggered.connect(self.save_session)
        self.session_menu.addAction(self.save_action)

        self.session_button = QToolButton(self)
        self.session_button.setText("Session")
        self.session_button.setMenu(self.session_menu)
        self.session_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        toolbar.addWidget(self.session_button)

        self.export_bdf_action = QAction("Export BDF", self)
        self.export_bdf_action.setStatusTip("Export selected channel measurements as BDF files")
        self.export_bdf_action.triggered.connect(self.export_bdf)
        toolbar.addAction(self.export_bdf_action)

        toolbar_spacer = QWidget(toolbar)
        toolbar_spacer.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        toolbar.addWidget(toolbar_spacer)

        self.debug_device_checkbox = QCheckBox("Debug device", toolbar)
        self.debug_device_checkbox.setToolTip(
            "Use a mock 9-channel test device when scanning"
        )
        toolbar.addWidget(self.debug_device_checkbox)

        self.channel_status_label = QLabel("Select a channel to view its status", self)
        self.channel_status_label.setObjectName("channelStatus")
        self.statusBar().addPermanentWidget(self.channel_status_label, 1)

        self.connection_indicator = connection_indicator()
        self.statusBar().addPermanentWidget(self.connection_indicator)

        self.device_state.connected.connect(self.on_connect)
        self.device_state.disconnected.connect(self.on_disconnect)
        self.device_state.connection_changed.connect(self.update_connection)
        self.connection_service.connected.connect(self.on_service_connected)
        self.connection_service.connection_failed.connect(self.on_service_connection_failed)
        self.connection_service.disconnected.connect(self.on_service_disconnected)
        self.connection_service.status_received.connect(self.handle_channel_status)
        self.connection_service.measurement_progress.connect(self.handle_measurement_progress)
        self.connection_service.measurement_finished.connect(self.handle_measurement_finished)
        self.connection_service.measurement_failed.connect(self.handle_measurement_failed)

        self.panel_conainer = QWidget()
        self.panel_conainer.setObjectName("panelContainer")
        self.panel_layout = QGridLayout(self.panel_conainer)
        self.panel_layout.setContentsMargins(18, 18, 18, 18)
        self.panel_layout.setHorizontalSpacing(16)
        self.panel_layout.setVerticalSpacing(16)

        self.panel_scroll_area = QScrollArea()
        self.panel_scroll_area.setObjectName("panelScrollArea")
        self.panel_scroll_area.setWidgetResizable(True)
        self.panel_scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.panel_scroll_area.setWidget(self.panel_conainer)
        self.setCentralWidget(self.panel_scroll_area)

    def scan_devices(self):
        if self.device_state.is_connected or self.connection_service.is_running:
            QMessageBox.information(
                self,
                "Already connected",
                "Disconnect the current device before connecting another one.",
            )
            return

        try:
            devices = pslib.find_devices(debug_mode=self.debug_device_checkbox.isChecked())
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Scan failed",
                f"Device discovery failed:\n{exc}",
            )
            return

        if not devices:
            QMessageBox.warning(self, "Scan complete", "No devices found")
            return

        selected = None
        dialog = device_selection_dialog(devices, self)
        if dialog.exec():
            selected = dialog.selected_device
        if selected is not None:
            self.pending_device = selected
            self.connect_action.setEnabled(False)
            self.statusBar().showMessage(f"Connecting to {selected.name}...", 0)
            try:
                self.connection_service.start(selected.channels)
            except Exception as exc:
                self.pending_device = None
                self.connect_action.setEnabled(True)
                QMessageBox.critical(
                    self,
                    "Connection failed",
                    f"Could not start the PalmSens connection:\n{exc}",
                )

    def request_disconnect(self):
        if self.active_runs:
            QMessageBox.warning(
                self,
                "Measurement running",
                "Stop the active measurement before disconnecting the device.",
            )
            return

        if not self.device_state.is_connected:
            return

        answer = QMessageBox.question(
            self,
            "Disconnect device?",
            "Are you sure you want to disconnect the device?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        if not self.connection_service.stop(wait=True):
            QMessageBox.warning(
                self,
                "Disconnect failed",
                "Could not close the PalmSens connections. Try again.",
            )
            return
        self.device_state.clear_connected_device()

    def update_connection(self, is_connected: bool):
        self.disconnect_action.setEnabled(is_connected)
        self.connect_action.setEnabled(not is_connected)

    def on_service_connected(self):
        device = self.pending_device
        self.pending_device = None
        if device is None:
            self.connection_service.stop()
            return
        self.device_state.set_connected_device(device)
        self.statusBar().showMessage(f"Connected to {device.name}.", 5000)

    def on_service_connection_failed(self, error: str):
        self.pending_device = None
        self.connect_action.setEnabled(True)
        self.statusBar().showMessage("PalmSens connection failed.", 5000)
        QMessageBox.critical(
            self,
            "Connection failed",
            f"Could not connect to the PalmSens channels:\n{error}",
        )

    def on_service_disconnected(self):
        self.pending_device = None
        self.connect_action.setEnabled(True)
        self.device_state.clear_connected_device()

    def open_aurora_builder(self):
        project_dir = Path(__file__).parent.parent
        builder_module = "aurora_method_builder"
        started = QProcess.startDetached(sys.executable, ["-m", builder_module], str(project_dir))
        if not started:
            QMessageBox.warning(
                self,
                "Launch failed",
                "Could not start the Aurora builder. Ensure the project dependencies are installed.",
            )

    def on_connect(self, dev):
        self.connection_indicator.set_status(True, dev)
        for instrument in dev.channels:
            self.add_panel(self._panel_title(instrument), instrument=instrument)

    def on_disconnect(self):
        self.clear_panel_selection()
        self.connection_indicator.set_status(False)
        self.clear_panels()

    def open_session(self):
        if self.active_runs:
            QMessageBox.warning(
                self,
                "Measurement running",
                "Stop the active measurement before opening a session.",
            )
            return

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select a file",
            "",
            "Session Files (*.pssession)",
        )
        if not file_path:
            return

        measurements = pslib.load_session(file_path)
        if not self.panels:
            QMessageBox.warning(
                self,
                "No channels available",
                "Connect to a device before opening a session.",
            )
            return

        if len(measurements) > len(self.panels):
            QMessageBox.warning(
                self,
                "Too many measurements",
                (
                    f"The session contains {len(measurements)} measurements, but only "
                    f"{len(self.panels)} channel panels are available. Only the first "
                    f"{len(self.panels)} measurements will be loaded."
                ),
            )

        for index, measurement in enumerate(measurements[: len(self.panels)]):
            self.panels[index].graph.plot_measurement(measurement)
            self.panels[index].set_status_text(None)

    def save_session(self):
        measurements = self._measurements()
        if not measurements:
            QMessageBox.warning(self, "Save error", "No measurements to save")
            return

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save session",
            "",
            "Session Files (*.pssession)",
        )
        if not file_path:
            return

        pslib.save_session(file_path, measurements)

    def export_bdf(self):
        exportable_panels = self._exportable_panels()
        # if not exportable_panels:
            # QMessageBox.warning(self, "Export error", "No channel measurements available to export.")
            # return

        dialog = bdf_export_dialog(exportable_panels, self)
        if not dialog.exec():
            return

        output_dir = dialog.output_directory()
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            written_files = []
            selected_panels = dialog.selected_panels()
            cell_name = dialog.cell_name()
            cas_id = dialog.cas_id()
            out_type = dialog.selected_type()
            used_sequence_numbers = set()
            for panel in selected_panels:
                sequence_number = self._next_bdf_sequence_number(
                    output_dir,
                    cell_name,
                    cas_id,
                    out_type,
                    used_sequence_numbers,
                )
                used_sequence_numbers.add(sequence_number)
                filename_stem = self._bdf_export_stem(cell_name, cas_id, sequence_number)
                written_files.extend(
                    export_measurement_to_bdf_files(
                        panel.graph.measurement,
                        output_dir,
                        filename_stem,
                        out_type,
                        optional_quantity_keys=dialog.selected_optional_quantity_keys(),
                    )
                )
        except BdfExportError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", f"Failed to export BDF files:\n{exc}")
            return

        self.statusBar().showMessage(f"Exported {len(written_files)} BDF file(s).", 5000)
        QMessageBox.information(
            self,
            "Export complete",
            f"Exported {len(written_files)} BDF file(s) to:\n{output_dir}",
        )

    def add_panel(self, title=None, instrument=None):
        if title is None:
            title = f"Panel {len(self.panels) + 1}"

        panel = graph_panel(title, instrument=instrument)
        panel.run_requested.connect(lambda panel=panel: self.run_measurement(panel))
        panel.stop_requested.connect(lambda panel=panel: self.stop_measurement(panel))
        panel.selection_requested.connect(lambda panel=panel: self.select_panel(panel))
        panel.expand_requested.connect(
            lambda is_expanded, panel=panel: self.set_panel_expanded(panel, is_expanded)
        )
        self.panels.append(panel)
        self.refresh_panel_grid()
        return panel

    def select_panel(self, panel: graph_panel):
        if panel not in self.panels:
            return

        if self.selected_panel is panel:
            return

        if self.selected_panel is not None:
            self.selected_panel.set_selected(False)

        self.selected_panel = panel
        panel.set_selected(True)
        if panel in self.active_runs:
            self.channel_status_label.setText(f"{panel.base_title} | Measurement running")
        else:
            self._show_selected_channel_status()

    def clear_panel_selection(self):
        if self.selected_panel is not None:
            self.selected_panel.set_selected(False)
        self.selected_panel = None
        self.channel_statuses.clear()
        self.channel_status_label.setText("Select a channel to view its status")

    def handle_channel_status(self, instrument, status: channel_status_snapshot):
        panel = self._panel_for_instrument(instrument)
        if panel is None:
            return
        self.channel_statuses[panel] = status
        if panel is self.selected_panel and panel not in self.active_runs:
            self._show_selected_channel_status()

    def _panel_for_instrument(self, instrument):
        return next(
            (panel for panel in self.panels if panel.instrument is instrument),
            None,
        )

    def _show_selected_channel_status(self):
        panel = self.selected_panel
        if panel is None:
            self.channel_status_label.setText("Select a channel to view its status")
            return

        status = self.channel_statuses.get(panel)
        if status is None:
            if getattr(panel.instrument, "interface", None) == "mock":
                text = f"{panel.base_title} | Status unavailable for mock device"
            else:
                text = f"{panel.base_title} | Waiting for idle status..."
            self.channel_status_label.setText(text)
            return

        parts = [
            panel.base_title,
            status.device_state,
            f"Potential: {status.potential_v:.3f} V",
        ]
        if status.current_ua is not None:
            parts.append(f"Current: {status.current_ua:.3f} µA")
        self.channel_status_label.setText(" | ".join(parts))

    def set_panel_expanded(self, panel: graph_panel, is_expanded: bool):
        if panel not in self.panels:
            return

        if is_expanded:
            previous_panel = self.expanded_panel
            self.expanded_panel = panel
            if previous_panel is not None and previous_panel is not panel:
                previous_panel.set_expanded(False)
        elif self.expanded_panel is panel:
            self.expanded_panel = None

        self.refresh_panel_grid()

    def refresh_panel_grid(self):
        for panel in self.panels:
            self.panel_layout.removeWidget(panel)

        if self.expanded_panel is not None and self.expanded_panel in self.panels:
            for panel in self.panels:
                is_expanded = panel is self.expanded_panel
                panel.set_expanded(is_expanded)
                panel.setVisible(is_expanded)

            self.panel_layout.addWidget(self.expanded_panel, 0, 0, 1, PANEL_COLUMNS)
            return

        for index, panel in enumerate(self.panels):
            panel.set_expanded(False)
            panel.show()
            row = index // PANEL_COLUMNS
            column = index % PANEL_COLUMNS
            self.panel_layout.addWidget(panel, row, column)
        for column in range(PANEL_COLUMNS):
            self.panel_layout.setColumnStretch(column, 1)

    def clear_panels(self):
        self.expanded_panel = None
        for panel in list(self.panels):
            self.panel_layout.removeWidget(panel)
            self.panels.remove(panel)
            panel.deleteLater()

    def run_measurement(self, panel: graph_panel):
        self.select_panel(panel)
        if panel.instrument is None:
            QMessageBox.warning(
                self,
                "No channel assigned",
                "Connect to a device and use one of its channel panels to run a measurement.",
            )
            return

        if panel in self.active_runs:
            return

        dialog = method_configuration_dialog(
            panel.base_title,
            instrument=panel.instrument,
            current_range_options=self.connection_service.current_range_options(panel.instrument),
            parent=self,
        )
        if not dialog.exec():
            return

        method = dialog.method
        method_label = dialog.method_label
        bdf_auto_save_settings = dialog.bdf_auto_save_settings
        if bdf_auto_save_settings is not None:
            try:
                bdf_auto_save_settings.output_dir.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                QMessageBox.warning(
                    self,
                    "BDF auto-save error",
                    f"Could not create BDF auto-save folder:\n{exc}",
                )
                return

        self.start_measurement(
            panel,
            method,
            method_label,
            dialog.temperature_settings,
            bdf_auto_save_settings,
        )

    def start_measurement(
        self,
        panel: graph_panel,
        method,
        method_label: str,
        temperature_settings=None,
        bdf_auto_save_settings: BdfAutoSaveSettings | None = None,
    ):
        run_id = self.next_run_id
        self.next_run_id += 1

        self.run_panels[run_id] = panel
        self.run_method_labels[run_id] = method_label
        if bdf_auto_save_settings is not None:
            self.run_bdf_auto_save_settings[run_id] = bdf_auto_save_settings
            self.run_bdf_auto_save_sequences[run_id] = set()

        self.active_runs[panel] = run_id
        panel.set_running(True)
        panel.set_status_text("Running")
        if panel is self.selected_panel:
            self.channel_status_label.setText(f"{panel.base_title} | Measurement running")
        self.statusBar().showMessage(f"Running {method_label} on {panel.base_title}...", 0)
        self.connection_service.start_measurement(
            run_id,
            panel.instrument,
            method,
            temperature_settings,
        )

    def stop_measurement(self, panel: graph_panel):
        run_id = self.active_runs.get(panel)
        if run_id is None:
            return

        self.stopping_panels.add(panel)
        panel.set_status_text("Stopping")
        self.statusBar().showMessage(f"Stopping measurement on {panel.base_title}...", 0)
        self.connection_service.abort_measurement(run_id)

    def handle_measurement_progress(self, run_id, callback_data):
        panel = self.run_panels.get(run_id)
        if panel is None or panel not in self.panels:
            return
        if isinstance(callback_data, LiveMeasurementStarted):
            panel.graph.begin_live_measurement(callback_data)
            return
        if isinstance(callback_data, AuroraStepCompleted):
            panel.graph.complete_live_segment(callback_data.segment)
            self.auto_save_aurora_step_bdf(run_id, panel, callback_data.segment)
            return
        if isinstance(callback_data, TemperatureProgress):
            panel.set_status_text(callback_data.message)
            self.statusBar().showMessage(f"{panel.base_title}: {callback_data.message}", 0)
            return
        panel.graph.plot_live_data(callback_data)

    def auto_save_aurora_step_bdf(self, run_id, panel: graph_panel, segment):
        settings = self.run_bdf_auto_save_settings.get(run_id)
        if settings is None:
            return

        try:
            if settings.custom_naming_enabled:
                filename_stem = _custom_bdf_export_stem(
                    settings.custom_base_name,
                    segment.index,
                    segment.step_type,
                    settings.include_step_type,
                )
            else:
                used_sequence_numbers = self.run_bdf_auto_save_sequences.setdefault(run_id, set())
                sequence_number = self._next_bdf_sequence_number(
                    settings.output_dir,
                    settings.cell_name,
                    settings.cas_id,
                    settings.export_type,
                    used_sequence_numbers,
                )
                used_sequence_numbers.add(sequence_number)
                filename_stem = self._bdf_export_stem(settings.cell_name, settings.cas_id, sequence_number)

            step_run = LogicalMeasurementRun(f"{panel.base_title} step {segment.index}", [segment])
            written_files = export_measurement_to_bdf_files(
                step_run,
                settings.output_dir,
                filename_stem,
                settings.export_type,
                optional_quantity_keys=settings.optional_quantity_keys,
            )
        except BdfExportError as exc:
            self._report_bdf_auto_save_failure(run_id, panel, str(exc))
            return
        except Exception as exc:
            self._report_bdf_auto_save_failure(run_id, panel, f"Failed to auto-save BDF files:\n{exc}")
            return

        self.statusBar().showMessage(
            f"Auto-saved Aurora step {segment.index} from {panel.base_title} as {len(written_files)} BDF file(s).",
            5000,
        )

    def _report_bdf_auto_save_failure(self, run_id, panel: graph_panel, message: str):
        self.statusBar().showMessage(f"BDF auto-save failed on {panel.base_title}.", 5000)
        if run_id in self.run_bdf_auto_save_failed:
            return
        self.run_bdf_auto_save_failed.add(run_id)
        QMessageBox.warning(
            self,
            "BDF auto-save failed",
            f"{panel.base_title} could not auto-save a BDF file:\n{message}",
        )

    def handle_measurement_finished(self, run_id, measurement):
        panel = self.run_panels.get(run_id)
        if panel is None:
            return
        method_label = self.run_method_labels.get(run_id, "Measurement")
        self.on_measurement_finished(panel, method_label, measurement)
        self.cleanup_run(panel, run_id)

    def handle_measurement_failed(self, run_id, error: str):
        panel = self.run_panels.get(run_id)
        if panel is None:
            return
        self.on_measurement_failed(panel, error)
        self.cleanup_run(panel, run_id)

    def on_measurement_finished(self, panel: graph_panel, method_label: str, measurement):
        if panel in self.stopping_panels:
            self.stopping_panels.discard(panel)
            panel.graph.plot_measurement(measurement)
            panel.set_status_text(None)
            self.statusBar().showMessage(f"Stopped measurement on {panel.base_title}.", 5000)
            return

        self.stopping_panels.discard(panel)
        panel.graph.plot_measurement(measurement)
        panel.set_status_text(None)
        self.statusBar().showMessage(
            f"Completed {method_label} on {panel.base_title}.",
            5000,
        )

    def on_measurement_failed(self, panel: graph_panel, error: str):
        panel.set_status_text(None)
        if panel in self.stopping_panels:
            self.stopping_panels.discard(panel)
            self.statusBar().showMessage(f"Stopped measurement on {panel.base_title}.", 5000)
            return

        self.statusBar().showMessage(f"Measurement failed on {panel.base_title}.", 5000)
        QMessageBox.critical(
            self,
            "Measurement failed",
            f"{panel.base_title} failed:\n{error}",
        )

    def cleanup_run(self, panel: graph_panel, run_id: int):
        if self.active_runs.get(panel) == run_id:
            self.active_runs.pop(panel, None)
        self.run_panels.pop(run_id, None)
        self.run_method_labels.pop(run_id, None)
        self.run_bdf_auto_save_settings.pop(run_id, None)
        self.run_bdf_auto_save_sequences.pop(run_id, None)
        self.run_bdf_auto_save_failed.discard(run_id)
        self.stopping_panels.discard(panel)
        if panel in self.panels:
            panel.set_running(False)
        if panel is self.selected_panel:
            self.channel_status_label.setText(
                f"{panel.base_title} | Waiting for idle status..."
            )

    def _measurements(self):
        return [
            panel.graph.measurement
            for panel in self.panels
            if panel.graph.measurement is not None
        ]

    def _exportable_panels(self):
        return [
            panel
            for panel in self.panels
            if panel.graph.measurement is not None
        ]

    @staticmethod
    def _sanitize_export_name(name: str) -> str:
        return _sanitize_filename_component(name)

    @staticmethod
    def _bdf_export_stem(cell_name: str, cas_id: str, sequence_number: int) -> str:
        return _default_bdf_export_stem(cell_name, cas_id, sequence_number)

    @classmethod
    def _next_bdf_sequence_number(
        cls,
        output_dir: Path,
        cell_name: str,
        cas_id: str,
        export_type: str,
        used_sequence_numbers: set[int],
    ) -> int:
        sequence_number = 1
        while sequence_number in used_sequence_numbers or cls._bdf_sequence_exists(
            output_dir,
            cell_name,
            cas_id,
            sequence_number,
            export_type,
        ):
            sequence_number += 1
        return sequence_number

    @classmethod
    def _bdf_sequence_exists(
        cls,
        output_dir: Path,
        cell_name: str,
        cas_id: str,
        sequence_number: int,
        export_type: str,
    ) -> bool:
        stem = cls._bdf_export_stem(cell_name, cas_id, sequence_number)
        return any(output_dir.glob(f"{stem}*.bdf.{export_type}"))

    @staticmethod
    def _panel_title(instrument):
        if getattr(instrument, "channel", -1) > 0: # Kolla om multichannel
            return f"CH {instrument.channel}"
        return instrument.name

    def closeEvent(self, event):
        if self.active_runs:
            QMessageBox.warning(
                self,
                "Measurement running",
                "Stop the active measurement before closing the application.",
            )
            event.ignore()
            return

        if not self.connection_service.stop(wait=True):
            QMessageBox.warning(
                self,
                "Disconnect failed",
                "Could not close the PalmSens connections.",
            )
            event.ignore()
            return
        super().closeEvent(event)


def main():
    app = QApplication()
    app.setStyleSheet(APP_STYLESHEET)
    window = main_window()
    window.show()
    app.exec()


if __name__ == "__main__":
    main()
