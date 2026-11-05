"""The 3D viewer's lighting controls: the Lighting list (presets, then HDRIs, then Browse) and the
bar that shows while an HDRI is in use (exposure, backdrop). They drive the viewport
(ui.viewer.Model3DViewportWidget), which reads the HDRI and draws it."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional, Sequence

import shiboken6
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QFileDialog, QHBoxLayout, QLabel, QPushButton, QSlider, QWidget

from core import archives, environment
from core.hdr_image import HDR_EXTENSIONS
from core.model_look import LIGHTINGS
from core.scanner import AssetItem
from core.settings import HDRI_EXPOSURE_RANGE, set_model_view
from ui.duotone import set_icon
from ui.tag_widgets import run_job

_BROWSE = "browse"
_HDRI_PREFIX = "hdri:"
_SLIDER_STEPS = 10  # per stop


def hdr_keys(assets: Sequence[AssetItem]) -> list[str]:
    """The paths of the HDR images among these."""
    return [str(asset.path) for asset in assets if asset.extension.lower() in HDR_EXTENSIONS]


class RoughnessBar(QWidget):
    """Metal's roughness: shown while the material is metal."""

    def __init__(self, viewport, parent: QWidget):
        super().__init__(parent)
        self.viewport = viewport
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        label = QLabel("Roughness")
        label.setObjectName("toolbarLabel")
        row.addWidget(label)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setValue(round(viewport.roughness * 100))
        self.slider.setFixedWidth(100)
        self.slider.setToolTip("How rough the metal is: 0 is a mirror, 100 is a very soft sheen")
        row.addWidget(self.slider)
        self.value = QLabel()
        self.value.setObjectName("toolbarLabel")
        row.addWidget(self.value)
        self._show_value()
        self.slider.valueChanged.connect(self._moved)
        self.slider.sliderReleased.connect(self._remember)

    def show_for_material(self) -> None:
        self.setVisible(self.viewport.material().metal > 0)

    def _show_value(self) -> None:
        self.value.setText(f"{self.slider.value()}")

    def _moved(self, value: int) -> None:
        self._show_value()
        dragging = self.slider.isSliderDown()  # (kept when it's let go: not on every step of a drag)
        self.viewport.set_roughness(value / 100, remember=not dragging)

    def _remember(self) -> None:
        set_model_view(metal_roughness=self.slider.value() / 100)


class LightingControls:
    """Fills combo (the toolbar's Lighting list) and builds bar for one viewport. find_assets gives
    the library's files: the HDRIs among them are found in the background and added to the list."""

    def __init__(self, viewport, combo: QComboBox, parent: QWidget, find_assets: Optional[Callable[[], Sequence[AssetItem]]] = None):
        """parent: the bar's (it's shown and hidden from the start: it must never be a window of its own)."""
        self.viewport = viewport
        self.combo = combo
        self._panoramas: list[environment.Panorama] = []
        self.bar = self._build_bar(parent)
        self._fill()
        combo.setToolTip("The lighting: a preset, or an HDRI from your library (or Browse...)")
        # (one width whatever's in it: the list fills up later, and HDRI names can be long)
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(14)
        combo.currentIndexChanged.connect(self._picked)
        viewport.lighting_changed.connect(self._show_current)
        self._show_current()
        if find_assets is not None:
            self._find(hdr_keys(find_assets()))

    # ------------------ the list ------------------
    def _fill(self) -> None:
        combo = self.combo
        combo.blockSignals(True)
        combo.clear()
        for lighting_id, lighting in LIGHTINGS.items():
            combo.addItem(lighting.label, lighting_id)
            combo.setItemData(combo.count() - 1, lighting.tip, Qt.ItemDataRole.ToolTipRole)
        panoramas = {p.key: (p.name, f"{p.width} x {p.height}, {self._where(p.key)}") for p in self._panoramas}
        current = self.viewport.hdri_key
        if current and current not in panoramas:  # (browsed to, or not found in the library yet)
            panoramas[current] = (environment.display_name(current), self._where(current))
        if panoramas:
            combo.insertSeparator(combo.count())
            presets = {lighting.label.casefold() for lighting in LIGHTINGS.values()}
            for key, (name, tip) in panoramas.items():
                combo.addItem(f"{name} (HDRI)" if name.casefold() in presets else name, _HDRI_PREFIX + key)
                combo.setItemData(combo.count() - 1, tip, Qt.ItemDataRole.ToolTipRole)
        combo.insertSeparator(combo.count())
        combo.addItem("Browse for an HDRI...", _BROWSE)
        combo.setItemData(combo.count() - 1, "Light the model with any .hdr or .exr panorama", Qt.ItemDataRole.ToolTipRole)
        combo.blockSignals(False)
        widest = max((combo.fontMetrics().horizontalAdvance(combo.itemText(i)) for i in range(combo.count())), default=0)
        combo.view().setMinimumWidth(widest + 48)  # (a long HDRI's name shows whole in the open list)
        self._show_current()

    @staticmethod
    def _where(key: str) -> str:
        archive, separator, name = key.partition(archives.SEPARATOR)
        return f"{name} inside {archive}" if separator else key

    def _show_current(self) -> None:
        """The list and the bar follow what the viewport is using."""
        viewport = self.viewport
        data = _HDRI_PREFIX + viewport.hdri_key if viewport.hdri_key else viewport.lighting_id
        index = self.combo.findData(data)
        if index < 0:  # (an HDRI that isn't in the list yet)
            self._fill()
            return
        self.combo.blockSignals(True)
        self.combo.setCurrentIndex(index)
        self.combo.blockSignals(False)
        self.bar.setVisible(bool(viewport.hdri_key))

    def _picked(self, index: int) -> None:
        data = self.combo.itemData(index)
        if data == _BROWSE:
            self._browse()
        elif isinstance(data, str) and data.startswith(_HDRI_PREFIX):
            self.viewport.use_hdri(data[len(_HDRI_PREFIX):])
        else:
            self.viewport.set_lighting(data)
        self._show_current()

    def _browse(self) -> None:
        patterns = " ".join(f"*{extension}" for extension in HDR_EXTENSIONS)
        path, _filter = QFileDialog.getOpenFileName(self.combo, "Light the Model With an HDRI", "", f"HDR panoramas ({patterns})")
        if path:
            self.viewport.use_hdri(str(Path(path)))

    def _find(self, keys: list[str]) -> None:
        """Files on disk first, as their headers are quick; those in archives may take a while."""
        on_disk = [key for key in keys if archives.SEPARATOR not in key]
        in_archives = [key for key in keys if archives.SEPARATOR in key]

        def found(panoramas: list[environment.Panorama]) -> None:
            if not shiboken6.isValid(self.combo):
                return
            self._panoramas = panoramas
            self._fill()

        def found_on_disk(panoramas: list[environment.Panorama]) -> None:
            found(panoramas)
            if in_archives and shiboken6.isValid(self.combo):
                run_job(lambda: environment.find_panoramas(on_disk + in_archives), found)

        run_job(lambda: environment.find_panoramas(on_disk), found_on_disk)

    # ------------------ the bar ------------------
    def _build_bar(self, parent: QWidget) -> QWidget:
        viewport = self.viewport
        bar = QWidget(parent)
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        label = QLabel("Exposure")
        label.setObjectName("toolbarLabel")
        row.addWidget(label)
        low, high = HDRI_EXPOSURE_RANGE
        self.slider_exposure = QSlider(Qt.Orientation.Horizontal)
        self.slider_exposure.setRange(int(low * _SLIDER_STEPS), int(high * _SLIDER_STEPS))
        self.slider_exposure.setValue(round(viewport.exposure_stops * _SLIDER_STEPS))
        self.slider_exposure.setFixedWidth(120)
        self.slider_exposure.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.slider_exposure.setTickInterval(_SLIDER_STEPS)
        self.slider_exposure.setToolTip("Brighter or darker, in stops. 0 is the HDRI's own automatic exposure")
        row.addWidget(self.slider_exposure)
        self.lbl_exposure = QLabel()
        self.lbl_exposure.setObjectName("toolbarLabel")
        row.addWidget(self.lbl_exposure)
        self.slider_exposure.valueChanged.connect(self._exposure_moved)
        self.slider_exposure.sliderReleased.connect(self._remember_exposure)
        self._show_exposure()

        self.btn_backdrop = QPushButton("Backdrop")
        set_icon(self.btn_backdrop, "backdrop")
        self.btn_backdrop.setCheckable(True)
        self.btn_backdrop.setChecked(viewport.show_backdrop)
        self.btn_backdrop.setToolTip("Show the HDRI behind the model (B)")
        self.btn_backdrop.toggled.connect(viewport.set_backdrop)
        viewport.backdrop_changed.connect(self.btn_backdrop.setChecked)
        row.addWidget(self.btn_backdrop)
        row.addStretch(1)
        return bar

    def _show_exposure(self) -> None:
        self.lbl_exposure.setText(f"{self.slider_exposure.value() / _SLIDER_STEPS:+.1f}")

    def _exposure_moved(self, value: int) -> None:
        self._show_exposure()
        dragging = self.slider_exposure.isSliderDown()  # (kept when it's let go: not on every step of a drag)
        self.viewport.set_exposure(value / _SLIDER_STEPS, remember=not dragging)

    def _remember_exposure(self) -> None:
        set_model_view(hdri_exposure=self.slider_exposure.value() / _SLIDER_STEPS)
