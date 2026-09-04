# -*- coding: utf-8 -*-
"""
gui_main.py

한국석유관리원 시험팀용 "자동차용 경유 가짜석유 원료 및 혼합비율 역추적/시뮬레이션"
데스크톱 프로그램 메인 GUI (PySide6 기반).

- 탭 1: 원료 분석 & 배합 시뮬레이션 (Case 1)
    * 경유/원료/가짜석유 GC 엑셀 파일 선택 및 물성치 입력
    * SLSQP 기반 혼합비율 역추적 버튼
    * 슬라이더로 경유:원료 비율을 조절하며 예상 GC 파형(오버레이) 및 물성치 실시간 갱신

- 탭 2: 미지 원료 추정 & DB 탐색 (Case 2)
    * 경유 및 가짜석유 파일/물성치 입력, 가정 혼합비율(a) 슬라이더
    * 역추정된 미지 원료 파형 표시
    * 원료 DB와의 코사인 유사도 기반 Top-3 후보 테이블 출력

실행:
    python gui_main.py
"""

from __future__ import annotations

import os
import sys
from typing import Optional

import numpy as np

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget, QVBoxLayout, QHBoxLayout,
    QFormLayout, QGroupBox, QLabel, QLineEdit, QPushButton, QFileDialog,
    QSlider, QDoubleSpinBox, QTableWidget, QTableWidgetItem, QMessageBox,
    QHeaderView, QSplitter, QDialog,
)

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from matplotlib import font_manager, rcParams

# --- Matplotlib 한글 폰트 설정 (Windows) ---
# 기본 폰트(DejaVu Sans)에는 한글이 없어 범례/제목이 깨지므로,
# 시스템에 설치된 한글 폰트(맑은 고딕 등)를 자동으로 찾아 적용한다.
def _setup_korean_font():
    preferred = ["Malgun Gothic", "맑은 고딕", "NanumGothic", "Gulim", "Dotum", "Batang"]
    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in preferred:
        if name in available:
            rcParams["font.family"] = name
            break
    else:
        # 못 찾으면 sans-serif 후보 중 한글 지원 가능성이 있는 것으로 폴백
        rcParams["font.family"] = "sans-serif"
        rcParams["font.sans-serif"] = preferred + rcParams.get("font.sans-serif", [])
    rcParams["axes.unicode_minus"] = False  # 한글 폰트에서 마이너스 부호 깨짐 방지

_setup_korean_font()

from data_parser import GCDataParser, GCWaveform
from analyzer import (
    FuelProperties, FuelSample, FuelBlendingSimulator, MixRatioEstimator,
    deconvolve_raw_waveform, estimate_unknown_raw_properties,
    match_fake_against_candidates,
)
from raw_material_db import RawMaterialDatabase, RawMaterialRecord, seed_example_records


# ---------------------------------------------------------------------------
# 공용 유틸: Matplotlib 캔버스 위젯
# ---------------------------------------------------------------------------
class MplCanvas(FigureCanvasQTAgg):
    """PySide6 QWidget에 임베드되는 Matplotlib 캔버스."""

    def __init__(self, parent=None, width=6, height=4, dpi=100):
        self.fig = Figure(figsize=(width, height), dpi=dpi, tight_layout=True)
        self.axes = self.fig.add_subplot(111)
        super().__init__(self.fig)
        self.setParent(parent)


# ---------------------------------------------------------------------------
# 물성치 입력 위젯 (재사용)
# ---------------------------------------------------------------------------
class PropertyInputGroup(QGroupBox):
    """식별제 함량 / 밀도 / 동점도 입력 필드를 묶은 그룹박스."""

    def __init__(self, title: str, parent=None):
        super().__init__(title, parent)
        layout = QFormLayout(self)

        self.marker_spin = QDoubleSpinBox()
        self.marker_spin.setRange(0.0, 100000.0)
        self.marker_spin.setDecimals(2)
        self.marker_spin.setSuffix(" mg/L")

        self.density_spin = QDoubleSpinBox()
        self.density_spin.setRange(0.0, 2.0)
        self.density_spin.setDecimals(4)
        self.density_spin.setSingleStep(0.001)
        self.density_spin.setSuffix(" g/cm3")

        self.viscosity_spin = QDoubleSpinBox()
        self.viscosity_spin.setRange(0.0, 1000.0)
        self.viscosity_spin.setDecimals(3)
        self.viscosity_spin.setSuffix(" mm2/s")

        layout.addRow("식별제 함량 (mg/L):", self.marker_spin)
        layout.addRow("밀도 15℃ (g/cm3):", self.density_spin)
        layout.addRow("동점도 40℃ (mm2/s):", self.viscosity_spin)

    def get_properties(self) -> FuelProperties:
        return FuelProperties(
            marker_conc=self.marker_spin.value(),
            density=self.density_spin.value(),
            viscosity=self.viscosity_spin.value(),
        )

    def set_properties(self, props: FuelProperties) -> None:
        self.marker_spin.setValue(props.marker_conc)
        self.density_spin.setValue(props.density)
        self.viscosity_spin.setValue(props.viscosity)


class DropLineEdit(QLineEdit):
    """파일 경로를 드래그앤드롭으로 받을 수 있는 QLineEdit."""

    file_dropped = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setPlaceholderText("파일을 선택하거나 여기로 드래그앤드롭하세요")

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if path:
                self.setText(path)
                self.file_dropped.emit(path)
        event.acceptProposedAction()


class FilePickerRow(QWidget):
    """파일 선택 버튼 + 경로 표시 라인(드래그앤드롭 지원)을 묶은 위젯.

    파일을 선택하면 `file_selected(str)` 시그널을 발생시켜, 부모 탭이
    즉시 해당 파형 미리보기를 갱신할 수 있도록 한다.
    """

    file_selected = Signal(str)

    def __init__(self, label_text: str, color: str = "tab:blue", parent=None):
        super().__init__(parent)
        self.color = color          # 이 행의 파형을 그릴 때 사용할 고정 색상
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel(label_text)
        # 라벨 텍스트 색상을 해당 파형 색상과 맞춰 직관적으로 표시
        self.label.setStyleSheet(f"color: {color}; font-weight: bold;")
        self.path_edit = DropLineEdit()
        self.path_edit.setReadOnly(False)
        self.path_edit.file_dropped.connect(self._on_dropped)
        self.browse_btn = QPushButton("파일 선택...")
        self.browse_btn.clicked.connect(self._on_browse)

        layout.addWidget(self.label)
        layout.addWidget(self.path_edit, stretch=1)
        layout.addWidget(self.browse_btn)

    def _on_browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "GC 크로마토그램 파일 선택", "",
            "Excel/CSV Files (*.xlsx *.xls *.csv *.txt *.tsv);;All Files (*)"
        )
        if path:
            self.path_edit.setText(path)
            self.file_selected.emit(path)

    def _on_dropped(self, path: str):
        self.file_selected.emit(path)

    def get_path(self) -> str:
        return self.path_edit.text().strip()


# ---------------------------------------------------------------------------
# 탭 1: Case 1 - 원료 분석 & 배합 시뮬레이션
# ---------------------------------------------------------------------------
class Case1Tab(QWidget):
    def __init__(self, parser: GCDataParser, parent=None):
        super().__init__(parent)
        self.parser = parser

        self.diesel_sample: Optional[FuelSample] = None
        self.raw_sample: Optional[FuelSample] = None
        self.fake_sample: Optional[FuelSample] = None
        self.simulator: Optional[FuelBlendingSimulator] = None

        # 파일 선택 즉시 표시용 미리보기 파형 캐시 (이름 -> (time, intensity, color, label))
        self._preview_waveforms: dict = {}

        self._build_ui()

    # ------------------------------------------------------------
    def _build_ui(self):
        main_layout = QHBoxLayout(self)

        # ---- 좌측: 입력 패널 ----
        left_panel = QVBoxLayout()

        input_group = QGroupBox("1. 데이터 입력")
        input_layout = QVBoxLayout(input_group)

        self.diesel_file_row = FilePickerRow("경유(Diesel) GC 파일:", color="green")
        self.raw_file_row = FilePickerRow("원료(Raw) GC 파일:", color="red")
        self.fake_file_row = FilePickerRow("가짜석유(Fake) GC 파일:", color="black")
        input_layout.addWidget(self.diesel_file_row)
        input_layout.addWidget(self.raw_file_row)
        input_layout.addWidget(self.fake_file_row)

        # 파일을 고르는 즉시 파형을 미리보기로 표시
        self.diesel_file_row.file_selected.connect(
            lambda p: self._update_preview("Diesel", p, "green", "경유(Diesel)"))
        self.raw_file_row.file_selected.connect(
            lambda p: self._update_preview("Raw", p, "red", "원료(Raw)"))
        self.fake_file_row.file_selected.connect(
            lambda p: self._update_preview("Fake", p, "black", "가짜석유(Fake)"))

        props_layout = QHBoxLayout()
        self.diesel_props_group = PropertyInputGroup("경유 물성치")
        self.raw_props_group = PropertyInputGroup("원료 물성치")
        self.fake_props_group = PropertyInputGroup("가짜석유 물성치 (실측)")
        props_layout.addWidget(self.diesel_props_group)
        props_layout.addWidget(self.raw_props_group)
        props_layout.addWidget(self.fake_props_group)
        input_layout.addLayout(props_layout)

        self.load_btn = QPushButton("데이터 로드 및 전처리")
        self.load_btn.clicked.connect(self.on_load_data)
        input_layout.addWidget(self.load_btn)

        left_panel.addWidget(input_group)

        # ---- 역추적 그룹 ----
        estimate_group = QGroupBox("2. 혼합비율 역추적 (SLSQP)")
        estimate_layout = QVBoxLayout(estimate_group)
        self.estimate_btn = QPushButton("혼합비율 역추적 실행")
        self.estimate_btn.clicked.connect(self.on_estimate_ratio)
        self.estimate_result_label = QLabel("결과: -")
        self.estimate_result_label.setWordWrap(True)
        estimate_layout.addWidget(self.estimate_btn)
        estimate_layout.addWidget(self.estimate_result_label)
        left_panel.addWidget(estimate_group)

        # ---- 시뮬레이션 슬라이더 ----
        sim_group = QGroupBox("3. 배합 시뮬레이션 (경유 비율 조절)")
        sim_layout = QVBoxLayout(sim_group)

        slider_row = QHBoxLayout()
        self.ratio_slider = QSlider(Qt.Horizontal)
        self.ratio_slider.setRange(0, 1000)
        self.ratio_slider.setValue(700)
        self.ratio_slider.valueChanged.connect(self.on_slider_changed)
        self.ratio_value_label = QLabel("경유 70.0% : 원료 30.0%")
        slider_row.addWidget(self.ratio_slider, stretch=1)
        sim_layout.addLayout(slider_row)
        sim_layout.addWidget(self.ratio_value_label)

        self.sim_props_label = QLabel("예상 물성치: -")
        self.sim_props_label.setWordWrap(True)
        sim_layout.addWidget(self.sim_props_label)

        left_panel.addWidget(sim_group)
        left_panel.addStretch(1)

        left_widget = QWidget()
        left_widget.setLayout(left_panel)

        # ---- 우측: 그래프 ----
        self.canvas = MplCanvas(self, width=7, height=6)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left_widget)
        splitter.addWidget(self.canvas)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        main_layout.addWidget(splitter)

    # ------------------------------------------------------------
    def _update_preview(self, key: str, path: str, color: str, label: str):
        """파일 선택 즉시 해당 파형을 독자 색상으로 미리보기 그래프에 표시."""
        try:
            wf = self.parser.load_file(path, name=key)
            self._preview_waveforms[key] = (wf.time, wf.intensity, color, label)
            self._redraw_preview()
        except Exception as e:
            QMessageBox.critical(self, "로드 오류", f"'{label}' 파일을 읽는 중 오류:\n{e}")

    def _redraw_preview(self):
        self.canvas.axes.clear()
        for key in ("Diesel", "Raw", "Fake"):
            if key in self._preview_waveforms:
                t, intensity, color, label = self._preview_waveforms[key]
                self.canvas.axes.plot(t, intensity, label=label, color=color,
                                       alpha=0.8, linewidth=0.8)
        self.canvas.axes.set_xlabel("Retention Time (min)")
        self.canvas.axes.set_ylabel("Intensity (raw)")
        self.canvas.axes.set_title("입력 데이터 미리보기 (파일 선택 즉시 표시)")
        if self._preview_waveforms:
            self.canvas.axes.legend(loc="upper right", fontsize=8)
        self.canvas.draw()

    # ------------------------------------------------------------
    def on_load_data(self):
        try:
            diesel_path = self.diesel_file_row.get_path()
            raw_path = self.raw_file_row.get_path()
            fake_path = self.fake_file_row.get_path()

            if not (diesel_path and raw_path and fake_path):
                QMessageBox.warning(self, "입력 오류", "경유/원료/가짜석유 GC 파일을 모두 선택해주세요.")
                return

            diesel_wf = self.parser.load_file(diesel_path, name="Diesel")
            raw_wf = self.parser.load_file(raw_path, name="Raw")
            fake_wf = self.parser.load_file(fake_path, name="Fake")

            # 공통 기준시간축 설정 후 베이스라인 보정 -> 리샘플링 -> 정규화
            self.parser.set_reference_time_from_data([diesel_wf, raw_wf, fake_wf])

            def process(wf: GCWaveform) -> GCWaveform:
                wf = self.parser.correct_baseline(wf)
                wf = self.parser.resample(wf)
                wf = self.parser.normalize(wf)
                return wf

            diesel_wf = process(diesel_wf)
            raw_wf = process(raw_wf)
            fake_wf = process(fake_wf)

            self.diesel_sample = FuelSample(
                "Diesel", diesel_wf.time, diesel_wf.intensity, self.diesel_props_group.get_properties())
            self.raw_sample = FuelSample(
                "Raw", raw_wf.time, raw_wf.intensity, self.raw_props_group.get_properties())
            self.fake_sample = FuelSample(
                "Fake", fake_wf.time, fake_wf.intensity, self.fake_props_group.get_properties())

            self.simulator = FuelBlendingSimulator(self.diesel_sample, self.raw_sample)

            self._plot_base_waveforms()
            QMessageBox.information(self, "완료", "데이터 로드 및 전처리가 완료되었습니다.")
            self.on_slider_changed(self.ratio_slider.value())

        except Exception as e:
            QMessageBox.critical(self, "오류", f"데이터 로드 중 오류가 발생했습니다:\n{e}")

    def _plot_base_waveforms(self):
        self.canvas.axes.clear()
        if self.diesel_sample:
            self.canvas.axes.plot(self.diesel_sample.time, self.diesel_sample.intensity,
                                   label="경유(Diesel)", color="green", alpha=0.7)
        if self.raw_sample:
            self.canvas.axes.plot(self.raw_sample.time, self.raw_sample.intensity,
                                   label="원료(Raw)", color="red", alpha=0.7)
        if self.fake_sample:
            self.canvas.axes.plot(self.fake_sample.time, self.fake_sample.intensity,
                                   label="가짜석유(Fake, 실측)", color="black", linewidth=1.5)
        self.canvas.axes.set_xlabel("Retention Time (min)")
        self.canvas.axes.set_ylabel("Normalized Intensity")
        self.canvas.axes.legend(loc="upper right", fontsize=8)
        self.canvas.draw()

    def on_estimate_ratio(self):
        if not (self.diesel_sample and self.raw_sample and self.fake_sample):
            QMessageBox.warning(self, "입력 오류", "먼저 데이터를 로드해주세요.")
            return
        try:
            estimator = MixRatioEstimator(self.diesel_sample, self.raw_sample, self.fake_sample)
            result = estimator.estimate(initial_guess=self.ratio_slider.value() / 1000.0)

            a_opt = result["a_optimal"]
            self.estimate_result_label.setText(
                f"추정 혼합비율: 경유 {a_opt*100:.2f}% : 원료 {(1-a_opt)*100:.2f}%  "
                f"(수렴={'성공' if result['success'] else '실패'}, cost={result['final_cost']:.3e})"
            )
            # 슬라이더를 추정 결과로 이동시켜 시뮬레이션 그래프 동기화
            self.ratio_slider.setValue(int(round(a_opt * 1000)))
            self.on_slider_changed(self.ratio_slider.value())

        except Exception as e:
            QMessageBox.critical(self, "오류", f"역추적 중 오류가 발생했습니다:\n{e}")

    def on_slider_changed(self, value: int):
        a = value / 1000.0
        self.ratio_value_label.setText(f"경유 {a*100:.1f}% : 원료 {(1-a)*100:.1f}%")

        if not self.simulator:
            return

        est_wave, est_props = self.simulator.simulate(a)

        self.sim_props_label.setText(
            f"예상 물성치 — 식별제: {est_props.marker_conc:.2f} mg/L, "
            f"밀도: {est_props.density:.4f} g/cm3, 동점도: {est_props.viscosity:.3f} mm2/s"
        )

        self._plot_base_waveforms()
        self.canvas.axes.plot(self.diesel_sample.time, est_wave,
                               label=f"예상 배합 (a={a:.2f})", color="purple", linestyle="--", linewidth=1.8)
        self.canvas.axes.legend(loc="upper right", fontsize=8)
        self.canvas.draw()


# ---------------------------------------------------------------------------
# 원료 후보 DB 관리: 추가/편집 폼 + 목록 다이얼로그
# ---------------------------------------------------------------------------
class RawMaterialRecordEditor(QDialog):
    """원료 후보 1건 추가/편집용 폼 다이얼로그.

    GC 크로마토그램은 파일 선택 버튼 또는 드래그앤드롭(FilePickerRow 재사용)으로
    받고, 유종/시료번호/식별제·밀도·동점도/비고를 함께 입력받는다.
    """

    def __init__(self, parser: GCDataParser, record: Optional[RawMaterialRecord] = None, parent=None):
        super().__init__(parent)
        self.parser = parser
        self.setWindowTitle("원료 후보 편집" if record else "원료 후보 추가")
        self.resize(480, 460)

        self._record_id: Optional[str] = record.id if record else None
        self._new_wf: Optional[GCWaveform] = None       # 새로 선택된 크로마토그램 (원본, 베이스라인 보정만 적용)
        self._existing_wave = None                       # 기존 레코드의 (time, intensity, source_filename)
        self.result_record: Optional[RawMaterialRecord] = None

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.oil_type_edit = QLineEdit()
        self.sample_no_edit = QLineEdit()
        self.notes_edit = QLineEdit()
        form.addRow("원료명:", self.name_edit)
        form.addRow("유종:", self.oil_type_edit)
        form.addRow("시료번호:", self.sample_no_edit)
        form.addRow("비고(기타 시험값):", self.notes_edit)
        layout.addLayout(form)

        self.props_group = PropertyInputGroup("물성치")
        layout.addWidget(self.props_group)

        file_group = QGroupBox("크로마토그램 (드래그앤드롭 또는 파일 선택)")
        file_layout = QVBoxLayout(file_group)
        self.file_row = FilePickerRow("GC 파일:", color="red")
        self.file_row.file_selected.connect(self._on_file_selected)
        file_layout.addWidget(self.file_row)
        self.file_status_label = QLabel("파일 없음")
        self.file_status_label.setStyleSheet("color: gray; font-size: 10px;")
        self.file_status_label.setWordWrap(True)
        file_layout.addWidget(self.file_status_label)
        layout.addWidget(file_group)

        if record:
            self.name_edit.setText(record.name)
            self.oil_type_edit.setText(record.oil_type)
            self.sample_no_edit.setText(record.sample_no)
            self.notes_edit.setText(record.notes)
            self.props_group.set_properties(record.properties)
            if record.has_waveform():
                self._existing_wave = (record.time, record.intensity, record.source_filename)
                self.file_status_label.setText(
                    f"기존 크로마토그램 사용 중 ({record.source_filename or '알 수 없음'}, "
                    f"{len(record.time)}포인트) — 새 파일을 선택하면 교체됩니다.")

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel_btn = QPushButton("취소")
        cancel_btn.clicked.connect(self.reject)
        save_btn = QPushButton("저장")
        save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

    def preload_file(self, path: str):
        """드래그앤드롭으로 다이얼로그 자체에 파일이 떨어졌을 때 미리 채워넣기 위한 진입점."""
        self.file_row.path_edit.setText(path)
        self._on_file_selected(path)

    def _on_file_selected(self, path: str):
        try:
            wf = self.parser.load_file(path)
            wf = self.parser.correct_baseline(wf)
            self._new_wf = wf
            self.file_status_label.setText(f"'{os.path.basename(path)}' 로드됨 ({len(wf.time)}포인트)")
            if not self.name_edit.text().strip():
                self.name_edit.setText(os.path.splitext(os.path.basename(path))[0])
        except Exception as e:
            QMessageBox.critical(self, "로드 오류", f"크로마토그램 로드 중 오류:\n{e}")
            self._new_wf = None

    def _on_save(self):
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "입력 오류", "원료명을 입력해주세요.")
            return

        if self._new_wf is not None:
            time_list = self._new_wf.time.tolist()
            intensity_list = self._new_wf.intensity.tolist()
            source_filename = os.path.basename(self._new_wf.source_path or "")
        elif self._existing_wave is not None:
            time_list, intensity_list, source_filename = self._existing_wave
        else:
            QMessageBox.warning(self, "입력 오류", "크로마토그램 파일을 선택해주세요.")
            return

        props = self.props_group.get_properties()
        kwargs = dict(
            name=name,
            oil_type=self.oil_type_edit.text().strip(),
            sample_no=self.sample_no_edit.text().strip(),
            marker_conc=props.marker_conc,
            density=props.density,
            viscosity=props.viscosity,
            notes=self.notes_edit.text().strip(),
            source_filename=source_filename,
            time=time_list,
            intensity=intensity_list,
        )
        if self._record_id:
            kwargs["id"] = self._record_id
        self.result_record = RawMaterialRecord(**kwargs)
        self.accept()


class RawMaterialDBDialog(QDialog):
    """원료 후보 DB 관리 창: 목록 조회, 추가/편집/삭제, 드래그앤드롭 업로드,
    JSON 가져오기/내보내기를 제공한다."""

    db_changed = Signal()

    def __init__(self, db: RawMaterialDatabase, parser: GCDataParser, parent=None):
        super().__init__(parent)
        self.db = db
        self.parser = parser
        self.setWindowTitle("원료 후보 DB 관리")
        self.resize(780, 480)
        self.setAcceptDrops(True)

        layout = QVBoxLayout(self)

        hint = QLabel(
            "GC 크로마토그램 파일을 이 창으로 드래그앤드롭하면 새 원료 후보 추가 창이 열립니다.\n"
            "※ 이름이 '[예시]'로 시작하는 항목은 최초 실행 시 자동 생성된 참고용 데이터입니다. "
            "'편집'으로 실제 유종/시료번호/시험값을 채우거나 '삭제' 후 실제 데이터를 추가하세요."
        )
        hint.setStyleSheet("color: gray; font-size: 10px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["원료명", "유종", "시료번호", "식별제(mg/L)", "밀도(g/cm3)", "동점도(mm2/s)"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.itemDoubleClicked.connect(lambda _: self.on_edit())
        layout.addWidget(self.table)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("추가...")
        add_btn.clicked.connect(self.on_add)
        edit_btn = QPushButton("편집...")
        edit_btn.clicked.connect(self.on_edit)
        del_btn = QPushButton("삭제")
        del_btn.clicked.connect(self.on_delete)
        import_btn = QPushButton("JSON 가져오기...")
        import_btn.clicked.connect(self.on_import)
        export_btn = QPushButton("JSON 내보내기...")
        export_btn.clicked.connect(self.on_export)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(edit_btn)
        btn_row.addWidget(del_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(import_btn)
        btn_row.addWidget(export_btn)
        layout.addLayout(btn_row)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = QPushButton("닫기")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        layout.addLayout(close_row)

        self._refresh_table()

    # -- 드래그앤드롭으로 새 후보 추가 -------------------------------------
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.toLocalFile()]
        event.acceptProposedAction()
        for path in paths:
            editor = RawMaterialRecordEditor(self.parser, parent=self)
            editor.preload_file(path)
            if editor.exec() == QDialog.Accepted and editor.result_record:
                self._add_record_safely(editor.result_record)

    # -- 테이블 표시 ---------------------------------------------------------
    def _refresh_table(self):
        self.table.setRowCount(len(self.db.records))
        for row, r in enumerate(self.db.records):
            values = [r.name, r.oil_type, r.sample_no,
                      f"{r.marker_conc:.2f}", f"{r.density:.4f}", f"{r.viscosity:.3f}"]
            for col, v in enumerate(values):
                item = QTableWidgetItem(v)
                if col == 0:
                    item.setData(Qt.UserRole, r.id)
                self.table.setItem(row, col, item)

    def _selected_record(self) -> Optional[RawMaterialRecord]:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        rec_id = self.table.item(rows[0].row(), 0).data(Qt.UserRole)
        return self.db.get(rec_id)

    # -- 버튼 핸들러 -----------------------------------------------------------
    def _add_record_safely(self, record: RawMaterialRecord):
        try:
            self.db.add(record)
        except OSError as e:
            QMessageBox.critical(self, "저장 오류", f"DB 파일에 저장하는 중 오류가 발생했습니다:\n{e}")
            return
        self._refresh_table()
        self.db_changed.emit()

    def on_add(self):
        editor = RawMaterialRecordEditor(self.parser, parent=self)
        if editor.exec() == QDialog.Accepted and editor.result_record:
            self._add_record_safely(editor.result_record)

    def on_edit(self):
        record = self._selected_record()
        if record is None:
            QMessageBox.information(self, "선택 없음", "편집할 항목을 목록에서 선택해주세요.")
            return
        editor = RawMaterialRecordEditor(self.parser, record=record, parent=self)
        if editor.exec() == QDialog.Accepted and editor.result_record:
            try:
                self.db.update(editor.result_record)
            except (OSError, KeyError) as e:
                QMessageBox.critical(self, "저장 오류", f"수정 내용을 저장하는 중 오류가 발생했습니다:\n{e}")
                return
            self._refresh_table()
            self.db_changed.emit()

    def on_delete(self):
        record = self._selected_record()
        if record is None:
            QMessageBox.information(self, "선택 없음", "삭제할 항목을 목록에서 선택해주세요.")
            return
        reply = QMessageBox.question(
            self, "삭제 확인", f"'{record.name}' 항목을 삭제하시겠습니까?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply == QMessageBox.Yes:
            try:
                self.db.delete(record.id)
            except OSError as e:
                QMessageBox.critical(self, "저장 오류", f"삭제 내용을 저장하는 중 오류가 발생했습니다:\n{e}")
                return
            self._refresh_table()
            self.db_changed.emit()

    def on_import(self):
        path, _ = QFileDialog.getOpenFileName(self, "원료 DB JSON 가져오기", "", "JSON Files (*.json)")
        if not path:
            return
        try:
            added = self.db.import_json(path)
            self._refresh_table()
            self.db_changed.emit()
            QMessageBox.information(self, "완료", f"{added}건을 가져왔습니다.")
        except Exception as e:
            QMessageBox.critical(self, "오류", f"가져오기 중 오류가 발생했습니다:\n{e}")

    def on_export(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "원료 DB JSON 내보내기", "raw_material_db.json", "JSON Files (*.json)")
        if not path:
            return
        try:
            self.db.export_json(path)
            QMessageBox.information(self, "완료", "내보내기가 완료되었습니다.")
        except Exception as e:
            QMessageBox.critical(self, "오류", f"내보내기 중 오류가 발생했습니다:\n{e}")


# ---------------------------------------------------------------------------
# 탭 2: Case 2 - 미지 원료 추정 & DB 탐색
# ---------------------------------------------------------------------------
class Case2Tab(QWidget):
    def __init__(self, parser: GCDataParser, parent=None):
        super().__init__(parent)
        self.parser = parser

        self.diesel_sample: Optional[FuelSample] = None
        self.fake_sample: Optional[FuelSample] = None

        # 원료 후보 DB (영속 저장, 추가/편집/삭제는 DB 관리 창에서).
        # DB 파일을 읽거나 쓸 수 없는 환경(권한이 제한된 PC 등)이어도 앱 자체는
        # 반드시 떠야 하므로, 여기서 예외가 나도 삼키고 빈 DB로 계속 진행한다.
        self.db = RawMaterialDatabase()
        self._db_load_error: Optional[str] = None
        try:
            self.db.load()
            seed_example_records(self.db)
        except OSError as e:
            self._db_load_error = str(e)

        # 파일 선택 즉시 표시용 미리보기 파형 캐시
        self._preview_waveforms: dict = {}
        # 최근 DB 매칭 결과 (행 선택 시 상세 보기용)
        self._last_matches: list = []

        self._build_ui()
        self._update_db_summary()

    # ------------------------------------------------------------
    def _build_ui(self):
        main_layout = QHBoxLayout(self)
        left_panel = QVBoxLayout()

        input_group = QGroupBox("1. 데이터 입력")
        input_layout = QVBoxLayout(input_group)
        self.diesel_file_row = FilePickerRow("경유(Diesel) GC 파일:", color="green")
        self.fake_file_row = FilePickerRow("가짜석유(Fake) GC 파일:", color="black")
        input_layout.addWidget(self.diesel_file_row)
        input_layout.addWidget(self.fake_file_row)

        # 파일을 고르는 즉시 파형 미리보기 표시
        self.diesel_file_row.file_selected.connect(
            lambda p: self._update_preview("Diesel", p, "green", "경유(Diesel)"))
        self.fake_file_row.file_selected.connect(
            lambda p: self._update_preview("Fake", p, "black", "가짜석유(Fake)"))
        # 두 파일이 모두 선택되면 자동으로 전처리 + DB 매칭 실행
        self.diesel_file_row.file_selected.connect(self._maybe_auto_process)
        self.fake_file_row.file_selected.connect(self._maybe_auto_process)

        props_layout = QHBoxLayout()
        self.diesel_props_group = PropertyInputGroup("경유 물성치")
        self.fake_props_group = PropertyInputGroup("가짜석유 물성치 (실측)")
        props_layout.addWidget(self.diesel_props_group)
        props_layout.addWidget(self.fake_props_group)
        input_layout.addLayout(props_layout)

        self.load_btn = QPushButton("데이터 로드 및 전처리")
        self.load_btn.clicked.connect(self.on_load_data)
        input_layout.addWidget(self.load_btn)
        left_panel.addWidget(input_group)

        db_group = QGroupBox("2. 원료 후보 DB")
        db_layout = QVBoxLayout(db_group)
        self.db_summary_label = QLabel("")
        db_layout.addWidget(self.db_summary_label)
        db_manage_btn = QPushButton("원료 DB 관리 (추가/편집/삭제)...")
        db_manage_btn.clicked.connect(self.on_open_db_manager)
        db_layout.addWidget(db_manage_btn)
        left_panel.addWidget(db_group)

        match_group = QGroupBox("3. DB 후보 매칭 — 각 후보를 원료로 대입해 최적 비율을 자동 계산")
        match_layout = QVBoxLayout(match_group)
        self.match_btn = QPushButton("DB 후보 매칭 실행")
        self.match_btn.clicked.connect(self.on_match_candidates)
        match_layout.addWidget(self.match_btn)

        self.result_table = QTableWidget(0, 4)
        self.result_table.setHorizontalHeaderLabels(["순위", "원료명", "추정 경유비율", "재구성오차"])
        self.result_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.result_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.result_table.itemDoubleClicked.connect(lambda _: self.on_show_match_detail())
        match_layout.addWidget(self.result_table)

        self.show_detail_btn = QPushButton("선택 후보 상세 비교 보기")
        self.show_detail_btn.clicked.connect(self.on_show_match_detail)
        self.show_detail_btn.setEnabled(False)
        match_layout.addWidget(self.show_detail_btn)
        left_panel.addWidget(match_group)

        manual_group = QGroupBox("4. (참고용) 수동 비율로 미지 원료 개형 보기")
        manual_layout = QVBoxLayout(manual_group)
        manual_hint = QLabel(
            "DB에 맞는 후보가 없을 때, 비율을 직접 지정해 '있을 법한' 원료 파형 개형만 대략 살펴보는 보조 도구입니다.\n"
            "경유+가짜석유 파형만으로는 비율을 자동으로 정확히 알아낼 수 없으므로, 정확한 결과는 위의 DB 매칭을 사용하세요."
        )
        manual_hint.setStyleSheet("color: gray; font-size: 10px;")
        manual_hint.setWordWrap(True)
        manual_layout.addWidget(manual_hint)

        self.ratio_slider = QSlider(Qt.Horizontal)
        self.ratio_slider.setRange(1, 999)   # a=1(원료 0%) 정의불가 -> 상한 제한
        self.ratio_slider.setValue(650)
        self.ratio_slider.valueChanged.connect(self.on_slider_changed)
        self.ratio_value_label = QLabel("경유 65.0% (수동 설정)")
        manual_layout.addWidget(self.ratio_slider)
        manual_layout.addWidget(self.ratio_value_label)

        self.show_manual_btn = QPushButton("이 비율로 원료 개형 미리보기")
        self.show_manual_btn.clicked.connect(self.on_show_manual_preview)
        manual_layout.addWidget(self.show_manual_btn)
        left_panel.addWidget(manual_group)

        left_panel.addStretch(1)
        left_widget = QWidget()
        left_widget.setLayout(left_panel)

        self.canvas = MplCanvas(self, width=7, height=6)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left_widget)
        splitter.addWidget(self.canvas)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        main_layout.addWidget(splitter)

    # ------------------------------------------------------------
    def _update_db_summary(self):
        if self._db_load_error:
            self.db_summary_label.setText(
                f"⚠ DB 파일을 열 수 없습니다 ({self._db_load_error}). "
                f"이번 세션 동안은 임시로만 사용되며 저장되지 않습니다.")
            self.db_summary_label.setStyleSheet("color: #b00020;")
        else:
            self.db_summary_label.setText(f"등록된 원료 후보: {len(self.db.records)}건")

    def on_open_db_manager(self):
        dialog = RawMaterialDBDialog(self.db, self.parser, parent=self)
        dialog.db_changed.connect(self._update_db_summary)
        dialog.exec()
        self._update_db_summary()

    # ------------------------------------------------------------
    def _update_preview(self, key: str, path: str, color: str, label: str):
        """파일 선택 즉시 해당 파형을 독자 색상으로 미리보기 그래프에 표시."""
        try:
            wf = self.parser.load_file(path, name=key)
            self._preview_waveforms[key] = (wf.time, wf.intensity, color, label)
            self._redraw_preview()
        except Exception as e:
            QMessageBox.critical(self, "로드 오류", f"'{label}' 파일을 읽는 중 오류:\n{e}")

    def _maybe_auto_process(self):
        """경유와 가짜석유 파일이 모두 선택되면 자동으로 전처리 및 DB 매칭을 실행."""
        if self.diesel_file_row.get_path() and self.fake_file_row.get_path():
            self.on_load_data()  # 내부에서 DB에 후보가 있으면 매칭까지 자동 실행

    def _redraw_preview(self):
        self.canvas.axes.clear()
        for key in ("Diesel", "Fake"):
            if key in self._preview_waveforms:
                t, intensity, color, label = self._preview_waveforms[key]
                self.canvas.axes.plot(t, intensity, label=label, color=color,
                                       alpha=0.8, linewidth=0.8)
        self.canvas.axes.set_xlabel("Retention Time (min)")
        self.canvas.axes.set_ylabel("Intensity (raw)")
        self.canvas.axes.set_title("입력 데이터 미리보기 (파일 선택 즉시 표시)")
        if self._preview_waveforms:
            self.canvas.axes.legend(loc="upper right", fontsize=8)
        self.canvas.draw()

    def _plot_inputs(self):
        self.canvas.axes.clear()
        if self.diesel_sample:
            self.canvas.axes.plot(self.diesel_sample.time, self.diesel_sample.intensity,
                                   label="경유(Diesel)", color="green", alpha=0.7)
        if self.fake_sample:
            self.canvas.axes.plot(self.fake_sample.time, self.fake_sample.intensity,
                                   label="가짜석유(Fake)", color="black", linewidth=1.5)
        self.canvas.axes.set_xlabel("Retention Time (min)")
        self.canvas.axes.set_ylabel("Normalized Intensity")
        self.canvas.axes.legend(loc="upper right", fontsize=8)
        self.canvas.draw()

    def on_slider_changed(self, value: int):
        a = value / 1000.0
        self.ratio_value_label.setText(f"경유 {a*100:.1f}% (수동 설정)")

    def on_load_data(self):
        try:
            diesel_path = self.diesel_file_row.get_path()
            fake_path = self.fake_file_row.get_path()
            if not (diesel_path and fake_path):
                QMessageBox.warning(self, "입력 오류", "경유 및 가짜석유 GC 파일을 모두 선택해주세요.")
                return

            diesel_wf = self.parser.load_file(diesel_path, name="Diesel")
            fake_wf = self.parser.load_file(fake_path, name="Fake")
            self.parser.set_reference_time_from_data([diesel_wf, fake_wf])

            def process(wf: GCWaveform) -> GCWaveform:
                wf = self.parser.correct_baseline(wf)
                wf = self.parser.resample(wf)
                wf = self.parser.normalize(wf)
                return wf

            diesel_wf = process(diesel_wf)
            fake_wf = process(fake_wf)

            self.diesel_sample = FuelSample(
                "Diesel", diesel_wf.time, diesel_wf.intensity, self.diesel_props_group.get_properties())
            self.fake_sample = FuelSample(
                "Fake", fake_wf.time, fake_wf.intensity, self.fake_props_group.get_properties())

            self._plot_inputs()
            # 등록된 후보가 있으면 곧바로 DB 매칭까지 자동 실행
            if self.db.records:
                self.on_match_candidates()
        except Exception as e:
            QMessageBox.critical(self, "오류", f"데이터 로드 중 오류가 발생했습니다:\n{e}")

    # ------------------------------------------------------------
    def on_match_candidates(self):
        """DB의 각 원료 후보를 실제로 대입해 Case 1과 동일한 SLSQP로 최적 혼합비율을
        구하고, 재구성 오차가 작은 순으로 정렬해 보여준다."""
        if not (self.diesel_sample and self.fake_sample):
            QMessageBox.warning(self, "입력 오류", "먼저 데이터를 로드해주세요.")
            return

        candidates = self.db.to_candidates(self.parser.reference_time)
        if not candidates:
            QMessageBox.warning(
                self, "입력 오류",
                "원료 후보 DB가 비어 있습니다. '원료 DB 관리'에서 후보를 추가해주세요.")
            return

        try:
            matches = match_fake_against_candidates(
                self.diesel_sample, self.fake_sample, candidates, top_n=5)
        except Exception as e:
            QMessageBox.critical(self, "오류", f"DB 매칭 중 오류가 발생했습니다:\n{e}")
            return

        self._last_matches = matches

        self.result_table.setRowCount(len(matches))
        for row, m in enumerate(matches):
            self.result_table.setItem(row, 0, QTableWidgetItem(str(row + 1)))
            self.result_table.setItem(row, 1, QTableWidgetItem(m.candidate_name))
            self.result_table.setItem(row, 2, QTableWidgetItem(f"{m.a_optimal*100:.1f}%"))
            self.result_table.setItem(row, 3, QTableWidgetItem(f"{m.final_cost:.3e}"))
        self.show_detail_btn.setEnabled(bool(matches))

        if matches:
            best = matches[0]
            self.ratio_slider.setValue(int(round(best.a_optimal * 1000)))
            self._plot_match(best)
            QMessageBox.information(
                self, "매칭 완료",
                f"최적 후보: {best.candidate_name} (경유 {best.a_optimal*100:.1f}%, "
                f"재구성오차 {best.final_cost:.3e})\n"
                f"추정 원료 물성치 — 식별제: {best.estimated_properties.marker_conc:.2f} mg/L, "
                f"밀도: {best.estimated_properties.density:.4f} g/cm3, "
                f"동점도: {best.estimated_properties.viscosity:.3f} mm2/s"
            )
        else:
            QMessageBox.information(self, "결과 없음", "매칭 가능한 후보가 없습니다.")

    def _plot_match(self, match):
        """경유/가짜석유/후보 원본 파형 + 최적비율에서의 예상 배합을 함께 표시."""
        cand = match.candidate
        self._plot_inputs()
        if cand is not None:
            self.canvas.axes.plot(self.diesel_sample.time, cand.intensity,
                                   label=f"후보: {match.candidate_name}", color="red", alpha=0.6)
            cand_sample = FuelSample(match.candidate_name, self.diesel_sample.time, cand.intensity, cand.properties)
            blended, _ = FuelBlendingSimulator(self.diesel_sample, cand_sample).simulate(match.a_optimal)
            self.canvas.axes.plot(self.diesel_sample.time, blended,
                                   label=f"예상 배합 (a={match.a_optimal:.2f})",
                                   color="purple", linestyle="--", linewidth=1.8)
        self.canvas.axes.legend(loc="upper right", fontsize=8)
        self.canvas.draw()

    def on_show_match_detail(self):
        rows = self.result_table.selectionModel().selectedRows() if self.result_table.selectionModel() else []
        if not rows or not self._last_matches:
            QMessageBox.information(self, "선택 없음", "표에서 후보를 먼저 선택해주세요.")
            return
        match = self._last_matches[rows[0].row()]
        cand = match.candidate
        if cand is None:
            return

        dialog = QDialog(self)
        dialog.setWindowTitle(f"상세 비교 — {match.candidate_name}")
        dialog.resize(900, 550)
        layout = QVBoxLayout(dialog)

        canvas = MplCanvas(dialog, width=9, height=5)
        canvas.axes.plot(self.diesel_sample.time, self.diesel_sample.intensity,
                          label="경유(Diesel)", color="green", alpha=0.5, linewidth=0.8)
        canvas.axes.plot(self.fake_sample.time, self.fake_sample.intensity,
                          label="가짜석유(Fake, 실측)", color="black", linewidth=1.5)
        canvas.axes.plot(self.diesel_sample.time, cand.intensity,
                          label=f"등록 후보: {match.candidate_name}", color="red", alpha=0.6)
        cand_sample = FuelSample(match.candidate_name, self.diesel_sample.time, cand.intensity, cand.properties)
        blended, _ = FuelBlendingSimulator(self.diesel_sample, cand_sample).simulate(match.a_optimal)
        canvas.axes.plot(self.diesel_sample.time, blended,
                          label=f"예상 배합 (a={match.a_optimal:.2f})",
                          color="purple", linestyle="--", linewidth=1.8)
        canvas.axes.set_xlabel("Retention Time (min)")
        canvas.axes.set_ylabel("Normalized Intensity")
        canvas.axes.set_title(f"경유 {match.a_optimal*100:.1f}% : {match.candidate_name} "
                               f"{match.raw_ratio*100:.1f}% — 재구성오차 {match.final_cost:.3e}")
        canvas.axes.legend(loc="upper right", fontsize=9)
        canvas.draw()
        layout.addWidget(canvas)

        info = QLabel(
            f"추정 원료 물성치 — 식별제: {match.estimated_properties.marker_conc:.2f} mg/L, "
            f"밀도: {match.estimated_properties.density:.4f} g/cm3, "
            f"동점도: {match.estimated_properties.viscosity:.3f} mm2/s | "
            f"파형 유사도: {match.wave_similarity*100:.1f}%"
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        close_btn = QPushButton("닫기")
        close_btn.clicked.connect(dialog.accept)
        layout.addWidget(close_btn)
        dialog.exec()

    # ------------------------------------------------------------
    def on_show_manual_preview(self):
        """(참고용) 수동으로 지정한 비율에서의 미지 원료 개형을 역추정해 보여준다.
        자동 추정이 아니라 사용자가 직접 정한 a에 대한 단순 대수적 역산이므로,
        DB에 등록된 실제 후보가 없을 때의 대략적 참고용으로만 사용해야 한다."""
        if not (self.diesel_sample and self.fake_sample):
            QMessageBox.warning(self, "입력 오류", "먼저 데이터를 로드해주세요.")
            return

        a = self.ratio_slider.value() / 1000.0
        raw_est_wave = deconvolve_raw_waveform(
            self.fake_sample.intensity, self.diesel_sample.intensity, a)
        raw_est_props = estimate_unknown_raw_properties(
            a, self.fake_sample.properties, self.diesel_sample.properties)

        dialog = QDialog(self)
        dialog.setWindowTitle("(참고용) 수동 비율 원료 개형 미리보기")
        dialog.resize(900, 550)
        layout = QVBoxLayout(dialog)

        canvas = MplCanvas(dialog, width=9, height=5)
        canvas.axes.plot(
            self.diesel_sample.time, raw_est_wave,
            label=f"역추정 원료 개형 (경유비율 a={a:.2f}, 수동 지정)",
            color="red", linewidth=1.2,
        )
        canvas.axes.plot(self.diesel_sample.time, self.diesel_sample.intensity,
                          label="경유(Diesel)", color="green", alpha=0.35, linewidth=0.8)
        canvas.axes.plot(self.fake_sample.time, self.fake_sample.intensity,
                          label="가짜석유(Fake)", color="black", alpha=0.35, linewidth=0.8)
        canvas.axes.set_xlabel("Retention Time (min)")
        canvas.axes.set_ylabel("Normalized Intensity")
        canvas.axes.set_title(
            f"(참고용) R_est(t)=max(0,(Fake - a·Diesel)/(1-a)), a={a:.3f} — 자동 추정 아님")
        canvas.axes.legend(loc="upper right", fontsize=9)
        canvas.draw()
        layout.addWidget(canvas)

        info = QLabel(
            f"역추정 물성치(참고용) — 식별제: {raw_est_props.marker_conc:.2f} mg/L, "
            f"밀도: {raw_est_props.density:.4f} g/cm3, "
            f"동점도: {raw_est_props.viscosity:.3f} mm2/s"
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        close_btn = QPushButton("닫기")
        close_btn.clicked.connect(dialog.accept)
        layout.addWidget(close_btn)
        dialog.exec()


# ---------------------------------------------------------------------------
# 메인 윈도우
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("경유 가짜석유 원료 및 혼합비율 역추적/시뮬레이션 프로그램")
        self.resize(1400, 850)

        # 탭마다 별도의 GCDataParser 인스턴스를 사용한다. 하나를 공유하면 한 탭에서
        # 데이터를 로드할 때 갱신되는 기준 시간축(reference_time)이 다른 탭에도
        # 영향을 주는 의도치 않은 결합이 생긴다.
        tabs = QTabWidget()
        tabs.addTab(Case1Tab(GCDataParser()), "원료 분석 & 배합 시뮬레이션 (Case 1)")
        tabs.addTab(Case2Tab(GCDataParser()), "미지 원료 추정 & DB 탐색 (Case 2)")

        self.setCentralWidget(tabs)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
