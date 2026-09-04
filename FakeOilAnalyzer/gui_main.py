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

import json
import os
import sys
from typing import Optional

import numpy as np

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget, QVBoxLayout, QHBoxLayout,
    QFormLayout, QGroupBox, QLabel, QLineEdit, QPushButton, QFileDialog,
    QSlider, QDoubleSpinBox, QTableWidget, QTableWidgetItem, QMessageBox,
    QHeaderView, QSplitter, QComboBox, QDialog, QListWidget,
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
    RawMaterialCandidate, deconvolve_raw_waveform, estimate_unknown_raw_properties,
    search_similar_raw_materials, mix_properties, estimate_a_from_all_properties,
)


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
# 탭 2: Case 2 - 미지 원료 추정 & DB 탐색
# ---------------------------------------------------------------------------
class Case2Tab(QWidget):
    def __init__(self, parser: GCDataParser, parent=None):
        super().__init__(parent)
        self.parser = parser

        self.diesel_sample: Optional[FuelSample] = None
        self.fake_sample: Optional[FuelSample] = None
        self.candidate_db: list = []
        # 파일 선택 즉시 표시용 미리보기 파형 캐시
        self._preview_waveforms: dict = {}
        # 역추정 결과 저장 (별도 창 표시용)
        self.last_est_waveform: Optional[np.ndarray] = None
        self.last_est_props = None
        self.last_est_a: float = 0.0

        self._build_ui()
        self._load_default_db()

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
        # 두 파일이 모두 선택되면 자동으로 전처리 + 미지 원료 추정 실행
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

        db_group = QGroupBox("2. 원료 후보 DB (GC 파형 CSV/JSON)")
        db_layout = QVBoxLayout(db_group)

        hint = QLabel("원료 후보 크로마토그램 CSV 파일들을 아래 목록으로 드래그앤드롭하거나 버튼으로 추가하세요.")
        hint.setStyleSheet("color: gray; font-size: 10px;")
        db_layout.addWidget(hint)

        self.candidate_list = QListWidget()
        self.candidate_list.setAcceptDrops(True)
        self.candidate_list.setDragDropMode(QListWidget.DropOnly)
        self.candidate_list.setDefaultDropAction(Qt.CopyAction)
        self.candidate_list.setMaximumHeight(90)
        # 드롭 이벤트를 가로채어 파일 경로를 수집
        self.candidate_list.dragEnterEvent = self._cand_drag_enter
        self.candidate_list.dragMoveEvent = self._cand_drag_move
        self.candidate_list.dropEvent = self._cand_drop
        db_layout.addWidget(self.candidate_list)

        db_btn_row = QHBoxLayout()
        self.db_add_btn = QPushButton("CSV 파일 추가...")
        self.db_add_btn.clicked.connect(self.on_add_candidate_files)
        self.db_remove_btn = QPushButton("선택 항목 제거")
        self.db_remove_btn.clicked.connect(self.on_remove_candidate)
        self.db_clear_btn = QPushButton("목록 초기화")
        self.db_clear_btn.clicked.connect(self.on_clear_candidates)
        db_btn_row.addWidget(self.db_add_btn)
        db_btn_row.addWidget(self.db_remove_btn)
        db_btn_row.addWidget(self.db_clear_btn)
        db_layout.addLayout(db_btn_row)

        db_layout.addWidget(QLabel("또는 JSON DB 파일 일괄 로드:"))
        db_json_row = QHBoxLayout()
        self.db_path_edit = QLineEdit()
        self.db_path_edit.setPlaceholderText("원료 DB JSON 파일 경로 (선택사항)")
        self.db_browse_btn = QPushButton("JSON 불러오기...")
        self.db_browse_btn.clicked.connect(self.on_browse_db)
        db_json_row.addWidget(self.db_path_edit, stretch=1)
        db_json_row.addWidget(self.db_browse_btn)
        db_layout.addLayout(db_json_row)

        left_panel.addWidget(db_group)

        ratio_group = QGroupBox("3. 혼합비율 (a: 경유 비율) — 자동 추정 또는 수동 설정")
        ratio_layout = QVBoxLayout(ratio_group)

        auto_row = QHBoxLayout()
        self.auto_a_btn = QPushButton("식별제+밀도+동점도+파형으로 혼합비율 자동 추정")
        self.auto_a_btn.clicked.connect(self.on_auto_estimate_a)
        self.auto_a_label = QLabel("")
        auto_row.addWidget(self.auto_a_btn)
        auto_row.addWidget(self.auto_a_label, stretch=1)
        ratio_layout.addLayout(auto_row)

        self.ratio_slider = QSlider(Qt.Horizontal)
        self.ratio_slider.setRange(1, 999)   # a=1(원료 0%) 정의불가 -> 상한 제한
        self.ratio_slider.setValue(650)
        self.ratio_slider.valueChanged.connect(self.on_slider_changed)
        self.ratio_value_label = QLabel("경유 65.0% (수동 설정 또는 자동 추정 결과)")
        ratio_layout.addWidget(self.ratio_slider)
        ratio_layout.addWidget(self.ratio_value_label)
        left_panel.addWidget(ratio_group)

        search_group = QGroupBox("4. 미지 원료 파형 역추정 & DB 탐색")
        search_layout = QVBoxLayout(search_group)
        self.search_btn = QPushButton("역추정 및 유사 원료 탐색 실행")
        self.search_btn.clicked.connect(self.on_search)
        search_layout.addWidget(self.search_btn)

        self.show_est_btn = QPushButton("예상 원료 크로마토그램 별도 창에 보기")
        self.show_est_btn.clicked.connect(self.on_show_estimated_window)
        self.show_est_btn.setEnabled(False)
        search_layout.addWidget(self.show_est_btn)

        self.result_table = QTableWidget(0, 4)
        self.result_table.setHorizontalHeaderLabels(["순위", "원료명", "일치율(종합)", "파형 일치율(%)"])
        self.result_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        search_layout.addWidget(self.result_table)
        left_panel.addWidget(search_group)

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
    def _load_default_db(self):
        """예시 원료 DB (실제 운영시 JSON 파일로 대체 가능)."""
        t = np.linspace(self.parser.ref_time_start, self.parser.ref_time_end, self.parser.ref_time_points)

        def gauss(center_ratio, width, amp):
            center = t.min() + center_ratio * (t.max() - t.min())
            return amp * np.exp(-0.5 * ((t - center) / width) ** 2)

        self.candidate_db = [
            RawMaterialCandidate("등유 유사 원료 A", gauss(0.2, 0.8, 1.0) + gauss(0.5, 1.0, 0.3),
                                  FuelProperties(marker_conc=5.0, density=0.800, viscosity=1.5)),
            RawMaterialCandidate("용제 유사 원료 B", gauss(0.35, 1.0, 1.0),
                                  FuelProperties(marker_conc=2.0, density=0.780, viscosity=1.1)),
            RawMaterialCandidate("윤활기유 유사 원료 C", gauss(0.7, 1.5, 1.0) + gauss(0.9, 1.0, 0.5),
                                  FuelProperties(marker_conc=1.0, density=0.870, viscosity=8.0)),
            RawMaterialCandidate("혼합 용제 원료 D", gauss(0.4, 0.6, 0.7) + gauss(0.6, 0.7, 0.7),
                                  FuelProperties(marker_conc=3.0, density=0.820, viscosity=2.5)),
        ]

    # ---- 원료 후보 리스트 (드래그앤드롭) 관리 ---------------------------
    def _cand_drag_enter(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def _cand_drag_move(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def _cand_drop(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.toLocalFile()]
        self._add_candidate_paths(paths)
        event.acceptProposedAction()

    def _add_candidate_paths(self, paths):
        added = 0
        for p in paths:
            ext = os.path.splitext(p)[1].lower()
            if ext in (".csv", ".txt", ".tsv", ".xlsx", ".xls"):
                # 중복 방지
                existing = [self.candidate_list.item(i).data(Qt.UserRole)
                            for i in range(self.candidate_list.count())]
                if p not in existing:
                    self.candidate_list.addItem(os.path.basename(p))
                    self.candidate_list.item(self.candidate_list.count() - 1).setData(Qt.UserRole, p)
                    self.candidate_list.item(self.candidate_list.count() - 1).setToolTip(p)
                    added += 1
        if added:
            # 실제 후보 객체는 탐색 실행 시점에 파일을 로드해 생성
            self.candidate_db = []  # 파일 기반 목록을 쓰도록 초기화
            self.show_est_btn.setEnabled(self.show_est_btn.isEnabled())  # 상태 유지
            QMessageBox.information(self, "후보 추가", f"{added}개의 원료 후보 파일을 목록에 추가했습니다.")

    def on_add_candidate_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "원료 후보 GC 파일 선택 (여러 개 가능)", "",
            "Excel/CSV Files (*.xlsx *.xls *.csv *.txt *.tsv);;All Files (*)"
        )
        if paths:
            self._add_candidate_paths(paths)

    def on_remove_candidate(self):
        for item in self.candidate_list.selectedItems():
            self.candidate_list.takeItem(self.candidate_list.row(item))

    def on_clear_candidates(self):
        self.candidate_list.clear()

    def _load_candidates_from_list(self):
        """후보 목록 위젯에 등록된 파일들을 읽어 RawMaterialCandidate 리스트로 만든다."""
        candidates = []
        if self.candidate_list.count() == 0:
            return candidates
        # 후보들도 입력 샘플과 동일 기준축으로 리샘플링되어야 비교 가능
        ref_t = self.parser.reference_time
        for i in range(self.candidate_list.count()):
            path = self.candidate_list.item(i).data(Qt.UserRole)
            name = os.path.splitext(os.path.basename(path))[0]
            try:
                wf = self.parser.load_file(path, name=name)
                wf = self.parser.correct_baseline(wf)
                wf = self.parser.resample(wf, target_time=ref_t)
                wf = self.parser.normalize(wf)
                candidates.append(RawMaterialCandidate(name, wf.intensity, FuelProperties()))
            except Exception as e:
                print(f"[경고] 후보 '{name}' 로드 실패: {e}")
        return candidates

    def on_browse_db(self):
        path, _ = QFileDialog.getOpenFileName(self, "원료 DB JSON 파일 선택", "", "JSON Files (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            t = np.linspace(self.parser.ref_time_start, self.parser.ref_time_end, self.parser.ref_time_points)
            db = []
            for item in data:
                intensity = np.array(item["intensity"], dtype=float)
                if len(intensity) != len(t):
                    # 길이가 다르면 균등 리샘플 (간단 보간)
                    src_t = np.linspace(t.min(), t.max(), len(intensity))
                    intensity = np.interp(t, src_t, intensity)
                props = FuelProperties(
                    marker_conc=item.get("marker_conc", 0.0),
                    density=item.get("density", 0.0),
                    viscosity=item.get("viscosity", 0.0),
                )
                db.append(RawMaterialCandidate(item["name"], intensity, props))
            self.candidate_db = db
            self.db_path_edit.setText(path)
            self.candidate_list.clear()  # JSON 사용 시 파일 목록은 비움
            QMessageBox.information(self, "완료", f"원료 DB {len(db)}건을 로드했습니다.")
        except Exception as e:
            QMessageBox.critical(self, "오류", f"DB 로드 중 오류가 발생했습니다:\n{e}")

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
            # 경유+가짜석유 두 크로마토그램이 입력되면 즉시 미지 원료를 추정하여 별도 창에 표시
            self._auto_estimate_and_show()
        except Exception as e:
            QMessageBox.critical(self, "오류", f"데이터 로드 중 오류가 발생했습니다:\n{e}")

    def _auto_estimate_and_show(self):
        """
        경유와 가짜석유 두 크로마토그램만으로 미지 원료 파형을 자동 추정하고
        별도 창에 크로마토그램을 띄운다. (혼합비율 a는 자동 추정 사용)
        """
        try:
            # 1) 혼합비율 자동 추정 (식별제+밀도+동점도+파형 종합)
            result = estimate_a_from_all_properties(
                self.fake_sample.intensity,
                self.diesel_sample.intensity,
                self.fake_sample.properties,
                self.diesel_sample.properties,
            )
            a_opt = result["a_optimal"]
            self.ratio_slider.setValue(int(round(a_opt * 1000)))
            self.auto_a_label.setText(f"자동 추정: 경유 {a_opt*100:.1f}%")

            # 2) 미지 원료 파형 역추정
            raw_est_wave = deconvolve_raw_waveform(
                self.fake_sample.intensity, self.diesel_sample.intensity, a_opt)
            raw_est_props = estimate_unknown_raw_properties(
                a_opt, self.fake_sample.properties, self.diesel_sample.properties)

            self.last_est_waveform = raw_est_wave
            self.last_est_props = raw_est_props
            self.last_est_a = a_opt
            self.show_est_btn.setEnabled(True)

            # 3) 메인 그래프에도 역추정 파형 오버레이
            self._plot_inputs()
            self.canvas.axes.plot(self.diesel_sample.time, raw_est_wave,
                                   label=f"역추정 원료 파형 (a={a_opt:.2f})",
                                   color="red", linestyle="--", linewidth=1.8)
            self.canvas.axes.legend(loc="upper right", fontsize=8)
            self.canvas.draw()

            # 4) 별도 창으로 미지 원료 크로마토그램 표시
            self.on_show_estimated_window()
        except Exception as e:
            QMessageBox.critical(self, "자동 추정 오류", f"미지 원료 자동 추정 중 오류가 발생했습니다:\n{e}")

    def _update_preview(self, key: str, path: str, color: str, label: str):
        """파일 선택 즉시 해당 파형을 독자 색상으로 미리보기 그래프에 표시."""
        try:
            wf = self.parser.load_file(path, name=key)
            self._preview_waveforms[key] = (wf.time, wf.intensity, color, label)
            self._redraw_preview()
        except Exception as e:
            QMessageBox.critical(self, "로드 오류", f"'{label}' 파일을 읽는 중 오류:\n{e}")

    def _maybe_auto_process(self):
        """경유와 가짜석유 파일이 모두 선택되면 자동으로 전처리 및 미지 원료 추정을 실행."""
        if self.diesel_file_row.get_path() and self.fake_file_row.get_path():
            self.on_load_data()  # 내부에서 _auto_estimate_and_show()까지 호출됨

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
        self.ratio_value_label.setText(f"경유 {a*100:.1f}% (수동 설정 또는 자동 추정 결과)")

    def on_auto_estimate_a(self):
        """식별제+밀도+동점도+파형을 모두 활용해 혼합비율 a를 자동 추정."""
        if not (self.diesel_sample and self.fake_sample):
            QMessageBox.warning(self, "입력 오류", "먼저 데이터를 로드해주세요.")
            return
        try:
            result = estimate_a_from_all_properties(
                self.fake_sample.intensity,
                self.diesel_sample.intensity,
                self.fake_sample.properties,
                self.diesel_sample.properties,
            )
            a_opt = result["a_optimal"]
            self.ratio_slider.setValue(int(round(a_opt * 1000)))
            est = result.get("estimated_raw_properties")
            extra = ""
            if est is not None:
                extra = (f" | 추정 원료 물성: 식별제 {est.marker_conc:.1f} mg/L, "
                         f"밀도 {est.density:.4f}, 동점도 {est.viscosity:.2f}")
            self.auto_a_label.setText(f"자동 추정: 경유 {a_opt*100:.1f}%{extra}")
            self.on_slider_changed(self.ratio_slider.value())
        except Exception as e:
            QMessageBox.critical(self, "오류", f"자동 추정 중 오류가 발생했습니다:\n{e}")

    def on_show_estimated_window(self):
        """역추정된 미지 원료 크로마토그램을 별도 팝업 창에 크게 표시."""
        if self.last_est_waveform is None or self.diesel_sample is None:
            QMessageBox.warning(self, "결과 없음", "먼저 '역추정 및 유사 원료 탐색 실행'을 눌러주세요.")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("예상 미지 원료 크로마토그램")
        dialog.resize(900, 550)
        layout = QVBoxLayout(dialog)

        canvas = MplCanvas(dialog, width=9, height=5)
        canvas.axes.plot(
            self.diesel_sample.time, self.last_est_waveform,
            label=f"역추정 원료 파형 (경유비율 a={self.last_est_a:.2f})",
            color="red", linewidth=1.2,
        )
        # 비교를 위해 경유/가짜 파형도 흐리게 함께 표시
        canvas.axes.plot(self.diesel_sample.time, self.diesel_sample.intensity,
                          label="경유(Diesel)", color="green", alpha=0.35, linewidth=0.8)
        if self.fake_sample is not None:
            canvas.axes.plot(self.fake_sample.time, self.fake_sample.intensity,
                              label="가짜석유(Fake)", color="black", alpha=0.35, linewidth=0.8)
        canvas.axes.set_xlabel("Retention Time (min)")
        canvas.axes.set_ylabel("Normalized Intensity")
        canvas.axes.set_title(
            f"예상 미지 원료 크로마토그램 — R_est(t)=max(0,(Fake - a·Diesel)/(1-a)), a={self.last_est_a:.3f}")
        canvas.axes.legend(loc="upper right", fontsize=9)
        canvas.draw()
        layout.addWidget(canvas)

        if self.last_est_props is not None:
            info = QLabel(
                f"역추정 물성치 — 식별제: {self.last_est_props.marker_conc:.2f} mg/L, "
                f"밀도: {self.last_est_props.density:.4f} g/cm3, "
                f"동점도: {self.last_est_props.viscosity:.3f} mm2/s"
            )
            info.setWordWrap(True)
            layout.addWidget(info)

        close_btn = QPushButton("닫기")
        close_btn.clicked.connect(dialog.accept)
        layout.addWidget(close_btn)
        dialog.exec()

    def on_search(self):
        if not (self.diesel_sample and self.fake_sample):
            QMessageBox.warning(self, "입력 오류", "먼저 데이터를 로드해주세요.")
            return

        # 1) 후보 소스 결정: 파일 목록이 있으면 그것을 사용, 없으면 JSON/기본 DB
        candidates = []
        if self.candidate_list.count() > 0:
            candidates = self._load_candidates_from_list()
        if not candidates and self.candidate_db:
            candidates = self.candidate_db
        if not candidates:
            QMessageBox.warning(self, "입력 오류", "원료 후보 DB가 비어 있습니다. CSV 파일을 드래그앤드롭으로 추가하거나 JSON DB를 불러오세요.")
            return

        try:
            a = self.ratio_slider.value() / 1000.0

            raw_est_wave = deconvolve_raw_waveform(
                self.fake_sample.intensity, self.diesel_sample.intensity, a)
            raw_est_props = estimate_unknown_raw_properties(
                a, self.fake_sample.properties, self.diesel_sample.properties)

            # 별도 창 표시를 위해 결과 저장
            self.last_est_waveform = raw_est_wave
            self.last_est_props = raw_est_props
            self.last_est_a = a
            self.show_est_btn.setEnabled(True)

            self._plot_inputs()
            self.canvas.axes.plot(self.diesel_sample.time, raw_est_wave,
                                   label=f"역추정 원료 파형 (a={a:.2f})",
                                   color="red", linestyle="--", linewidth=1.8)
            self.canvas.axes.legend(loc="upper right", fontsize=8)
            self.canvas.draw()

            matches = search_similar_raw_materials(
                raw_est_wave, raw_est_props, candidates, top_n=5)

            self.result_table.setRowCount(len(matches))
            for row, m in enumerate(matches):
                self.result_table.setItem(row, 0, QTableWidgetItem(str(row + 1)))
                self.result_table.setItem(row, 1, QTableWidgetItem(m.candidate_name))
                self.result_table.setItem(row, 2, QTableWidgetItem(f"{m.combined_score*100:.1f}%"))
                self.result_table.setItem(row, 3, QTableWidgetItem(f"{m.similarity*100:.1f}%"))

            QMessageBox.information(
                self, "완료",
                f"역추정 물성치 — 식별제: {raw_est_props.marker_conc:.2f} mg/L, "
                f"밀도: {raw_est_props.density:.4f} g/cm3, 동점도: {raw_est_props.viscosity:.3f} mm2/s"
            )
        except Exception as e:
            QMessageBox.critical(self, "오류", f"탐색 중 오류가 발생했습니다:\n{e}")


# ---------------------------------------------------------------------------
# 메인 윈도우
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("경유 가짜석유 원료 및 혼합비율 역추적/시뮬레이션 프로그램")
        self.resize(1400, 850)

        self.parser = GCDataParser()

        tabs = QTabWidget()
        tabs.addTab(Case1Tab(self.parser), "원료 분석 & 배합 시뮬레이션 (Case 1)")
        tabs.addTab(Case2Tab(self.parser), "미지 원료 추정 & DB 탐색 (Case 2)")

        self.setCentralWidget(tabs)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
