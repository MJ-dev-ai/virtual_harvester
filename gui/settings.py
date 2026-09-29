"""Camera-type settings view. All GenApi access remains in DeviceManager."""

from copy import deepcopy
from functools import partial
from math import isfinite

from PySide6.QtCore import QSignalBlocker, Qt, Signal, Slot
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QLabel,
    QSpinBox, QWidget,
)

from config.path import UI_PATH


SETTING_WIDGETS = {
    "Camera": "comboCamera",
    "State": "labelState",
    "ExposureAuto": "comboExposureAuto",
    "ExposureTime": "spinExposureTime",
    "ExposureTimeSlider": "sliderExposureTime",
    "GainAuto": "comboGainAuto",
    "Gain": "spinGain",
    "GainSlider": "sliderGain",
    "PixelFormat": "comboPixelFormat",
    "Width": "spinWidth", "Height": "spinHeight",
    "OffsetX": "spinOffsetX", "OffsetY": "spinOffsetY",
    "BalanceWhiteAuto": "comboBalanceWhiteAuto",
    "AcquisitionFrameRateEnable": "checkAcquisitionFrameRateEnable",
    "AcquisitionFrameRate": "spinAcquisitionFrameRate",
    "TriggerMode": "comboTriggerMode",
    "TriggerSelector": "comboTriggerSelector",
    "TriggerSource": "comboTriggerSource",
    "TriggerActivation": "comboTriggerActivation",
    "Apply": "buttonApply",
    "Close": "buttonClose",
}

# These controls are part of the dialog, not GenApi setting nodes.
DIALOG_WIDGETS = {
    "Camera", "State", "ExposureTimeSlider", "GainSlider", "Apply", "Close",
}


# GUI object names never cross the camera signal boundary. Sliders share
# the same GenApi feature as their spin boxes.
WIDGET_FEATURES = {
    widget_name: feature for feature, widget_name in SETTING_WIDGETS.items()
    if feature not in DIALOG_WIDGETS
}
WIDGET_FEATURES.update(sliderExposureTime="ExposureTime", sliderGain="Gain")

# Display labels may change independently of GenApi enumeration symbols.
CHOICE_LABELS = {
    "PixelFormat": {"RGB8": "RGB24"},
    "TriggerMode": {"Off": "Disabled", "On": "Enabled"},
    "ExposureAuto": {"Off": "Off", "Once": "Once", "Continuous": "Continuous"},
    "GainAuto": {"Off": "Off", "Once": "Once", "Continuous": "Continuous"},
}


class _DialogLoader(QUiLoader):
    """Load Designer's root QDialog into the Python dialog instance."""

    def __init__(self, dialog):
        super().__init__()
        self.dialog = dialog

    def createWidget(self, class_name, parent=None, name=""):
        if parent is None and class_name == "QDialog":
            self.dialog.setObjectName(name)
            return self.dialog
        return super().createWidget(class_name, parent, name)


class SettingsDialog(QDialog):
    """Cache model-group metadata and preserve edits while switching groups.

    on_camera_settings_received accepts DeviceManager.camera_settings_received.
    groups contains the received snapshot; draft_values holds edits separately.
    Signal payloads use GenApi feature names and enumeration symbols only.
    """

    apply_requested = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        loader = _DialogLoader(self)
        if loader.load(str(UI_PATH / "settings.ui")) is None:
            raise RuntimeError(loader.errorString())
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.groups = []
        self.draft_values = []
        self._group_index = -1
        self.widgets = {
            key: self.findChild(QWidget, name)
            for key, name in SETTING_WIDGETS.items()
        }
        self.camera_combo = self.widgets["Camera"]
        self.state_label = self.widgets["State"]
        self.apply_button = self.widgets["Apply"]
        self.close_button = self.widgets["Close"]
        self.feature_widgets = {
            key: widget for key, widget in self.widgets.items()
            if key not in DIALOG_WIDGETS
        }
        self.sliders = {
            "ExposureTime": self.widgets["ExposureTimeSlider"],
            "Gain": self.widgets["GainSlider"],
        }
        self.camera_combo.currentIndexChanged.connect(self._select_group)
        self.close_button.clicked.connect(self.reject)
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self._apply)
        for feature, widget in self.feature_widgets.items():
            signal = (widget.currentIndexChanged if isinstance(widget, QComboBox)
                      else widget.stateChanged if isinstance(widget, QCheckBox)
                      else widget.valueChanged)
            signal.connect(partial(self._value_edited, feature))
            if isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                widget.setKeyboardTracking(False)
                widget.editingFinished.connect(partial(self._value_edited, feature))
        for feature, slider in self.sliders.items():
            # valueChanged also handles keyboard and track clicks, not just dragging.
            slider.valueChanged.connect(partial(self._slider_edited, feature))
        self.on_camera_settings_received({"success": True, "groups": []})

    @Slot(dict)
    def on_camera_settings_received(self, result):
        """Replace a camera snapshot; never retain edits for a previous connection."""
        self.groups = deepcopy(result.get("groups", [])) if result["success"] else []
        self.draft_values = [{} for _ in self.groups]
        self._group_index = -1
        with QSignalBlocker(self.camera_combo):
            self.camera_combo.clear()
            for group in self.groups:
                label = " / ".join((group["vendor"] or "Unknown vendor",
                                    group["model"] or "Unknown model"))
                self.camera_combo.addItem(f"{label} ({len(group['cameras'])} cameras)")
            self.camera_combo.setCurrentIndex(0 if self.groups else -1)
        self.camera_combo.setEnabled(bool(self.groups))
        self.apply_button.setEnabled(bool(self.groups))
        self._select_group(self.camera_combo.currentIndex())
        self.state_label.setText("Connected" if self.groups else "Disconnected")
        self.state_label.setToolTip(result.get("error") or "")

    @Slot(dict)
    def on_disconnected(self, result):
        # Even partial disconnection makes the old group membership unsafe to use.
        self.on_camera_settings_received({"success": True, "groups": []})
        self.reject()

    @Slot(int)
    def _select_group(self, index):
        self._group_index = index
        group = self.groups[index] if 0 <= index < len(self.groups) else None
        for feature, widget in self.feature_widgets.items():
            meta = group["settings"].get(feature, {}) if group else {}
            value = self.draft_values[index].get(feature, meta.get("value")) if group else None
            readable = bool(meta.get("supported") and meta.get("available")
                            and meta.get("readable") and not meta.get("error"))
            editable = readable and bool(meta.get("writable"))
            mixed = readable and meta.get("mixed", False) and value is None
            missing = "Mixed values" if mixed else "Unavailable"
            details = meta.get("error") or ("" if readable else "Not supported or unavailable")
            if readable and meta.get("type") in ("int", "float"):
                details = f"Range: {meta['min']} – {meta['max']} {meta.get('unit', '')}"
                if meta.get("inc"):
                    details += f"; increment: {meta['inc']}"
            if mixed:
                details += "; cameras have different current values"
            widget.setToolTip(details)
            with QSignalBlocker(widget):
                if isinstance(widget, QComboBox):
                    widget.clear()
                    widget.setPlaceholderText(missing)
                    for symbol in meta.get("choices", []) if readable else []:
                        label = CHOICE_LABELS.get(feature, {}).get(symbol, symbol)
                        widget.addItem(label, symbol)
                    widget.setCurrentIndex(widget.findData(value) if value is not None else -1)
                elif isinstance(widget, QCheckBox):
                    widget.setTristate(mixed)
                    widget.setCheckState(Qt.CheckState.PartiallyChecked if mixed else
                                         Qt.CheckState.Checked if value else Qt.CheckState.Unchecked)
                else:
                    if isinstance(widget, QDoubleSpinBox):
                        widget.setDecimals(6)
                    low, high = meta.get("min"), meta.get("max")
                    if (not readable or low is None or high is None
                            or not isfinite(low) or not isfinite(high) or low > high):
                        low = high = 0
                        editable = False
                    elif not editable and value is not None:
                        # Fixed/read-only GenApi integers may expose INT64 bounds.
                        low = high = value
                    if isinstance(widget, QSpinBox):
                        low, high = max(-2147483648, int(low)), min(2147483647, int(high))
                        if low > high:
                            low = high = 0
                            editable = False
                    widget.setRange(low, high)
                    widget.setSingleStep(meta.get("inc") or (1 if isinstance(widget, QSpinBox) else 0.1))
                    unit = meta.get("unit") or ("px" if feature in ("Width", "Height", "OffsetX", "OffsetY") else "")
                    widget.setSuffix(f" {unit}" if unit else "")
                    widget.setValue(value if value is not None and readable else low)
                    widget.lineEdit().setPlaceholderText(missing)
                    if value is None or not readable:
                        widget.clear()
                widget.setEnabled(editable)
            if feature in self.sliders:
                self._sync_slider(feature)
        source = self.widgets["TriggerSource"].currentText()
        self.findChild(QLabel, "labelTriggerSourceValue").setText(source or "—")

    def _sync_slider(self, feature):
        spin, slider = self.widgets[feature], self.sliders[feature]
        span = spin.maximum() - spin.minimum()
        with QSignalBlocker(slider):
            slider.setRange(0, 10000 if span > 0 else 0)
            slider.setValue(round((spin.value() - spin.minimum()) / span * 10000) if span > 0 else 0)
            slider.setEnabled(spin.isEnabled() and span > 0)
            slider.setToolTip(spin.toolTip())

    def _slider_edited(self, feature, position):
        if self._group_index < 0:
            return
        spin = self.widgets[feature]
        value = spin.minimum() + (spin.maximum() - spin.minimum()) * position / 10000
        meta = self.groups[self._group_index]["settings"][feature]
        if meta.get("inc"):
            value = meta["min"] + round((value - meta["min"]) / meta["inc"]) * meta["inc"]
        spin.setValue(value)

    def _value_edited(self, feature, *_):
        if self._group_index < 0:
            return
        widget = self.widgets[feature]
        if not widget.isEnabled():
            return
        if isinstance(widget, QComboBox):
            if widget.currentIndex() < 0:
                return
            value = widget.currentData()
        elif isinstance(widget, QCheckBox):
            if widget.checkState() == Qt.CheckState.PartiallyChecked:
                return
            value = widget.isChecked()
        else:
            value = widget.value()
            meta = self.groups[self._group_index]["settings"][feature]
            if meta.get("inc"):
                value = meta["min"] + round((value - meta["min"]) / meta["inc"]) * meta["inc"]
                value = min(meta["max"], max(meta["min"], value))
                with QSignalBlocker(widget):
                    widget.setValue(value)
                value = widget.value()
        camera_feature = WIDGET_FEATURES[widget.objectName()]
        self.draft_values[self._group_index][camera_feature] = value
        if feature in self.sliders:
            self._sync_slider(feature)

    @Slot()
    def _apply(self):
        # Commit text still being edited before taking the outgoing snapshot.
        for widget in self.feature_widgets.values():
            if isinstance(widget, (QSpinBox, QDoubleSpinBox)) and widget.isEnabled():
                widget.interpretText()
        pending = self.pending_settings()
        self.accept()
        self.apply_requested.emit(pending)

    def pending_settings(self):
        """Return changed values and target IDs for an Apply request."""
        groups = []
        for group, edits in zip(self.groups, self.draft_values):
            changes = {name: value for name, value in edits.items()
                       if group["settings"][name].get("mixed")
                       or value != group["settings"][name].get("value")}
            if changes:
                groups.append({
                    "vendor": group["vendor"], "model": group["model"],
                    "cameras": [{"framegrabber_id": camera["framegrabber_id"],
                                 "camera_id": camera["camera_id"]}
                                for camera in group["cameras"]],
                    "settings": changes,
                })
        return deepcopy({"groups": groups})
