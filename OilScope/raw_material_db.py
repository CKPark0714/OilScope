# -*- coding: utf-8 -*-
"""
raw_material_db.py

원료 후보 DB(유종/시료번호/기타 시험값 + GC 크로마토그램 원본 파형)를 로컬 JSON
파일로 영속 관리하는 모듈. gui_main.py의 원료 DB 관리 다이얼로그가 이 모듈을 통해
추가/편집/삭제(CRUD) 및 가져오기/내보내기를 수행한다.

레코드는 항상 "원본 시간축 그대로" 저장한다 (비교용 기준 시간축이 아니라).
비교(검색) 시점에 to_candidates()가 현재 비교 기준 시간축으로 실제 체류시간 값
기준 보간(resample)을 수행하므로, 기준 시간축이 나중에 바뀌어도(예: 다른 시료를
로드해 구간이 달라짐) 정합성이 깨지지 않는다.
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from dataclasses import dataclass, field, asdict
from typing import List, Optional

import numpy as np
import pandas as pd
from scipy.interpolate import interp1d

from analyzer import FuelProperties, RawMaterialCandidate, normalize_density_g_cm3
from data_parser import GCDataParser


def _program_dir() -> str:
    """프로그램 폴더 경로를 반환한다.

    PyInstaller로 패키징된 실행 파일에서는 sys.executable이 실제 .exe가 놓인
    위치를 가리킨다(onefile 빌드도 마찬가지 - sys._MEIPASS와 달리 sys.executable은
    임시 압축해제 폴더가 아니라 사용자가 둔 실제 위치다). 이렇게 해야 압축을 풀어
    어느 폴더에 두든 그 자리에서 바로 데이터 폴더가 함께 생성/관리된다(휴대용 배포
    방식). 스크립트로 직접 실행할 때는 이 파일이 있는 폴더를 프로그램 폴더로 본다.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


# 프로그램 폴더 바로 밑에 데이터 폴더(OilScopeData)를 만들고, 그 안에 DB 파일을
# 둔다. 사용자 홈 디렉터리의 숨김 폴더(~/.oilscope 등)에 두지 않는 이유: 압축을
# 풀거나 옮긴 프로그램 폴더 옆에서 바로 데이터가 보이고 관리되어야 하기 때문
# (USB로 통째로 복사해 옮겨도 데이터가 그대로 따라오는 휴대용 배포를 전제로 함).
DEFAULT_DB_PATH = os.path.join(_program_dir(), "OilScopeData", "raw_material_db.json")


@dataclass
class RawMaterialRecord:
    """원료 후보 1건: 식별 정보 + 물성치 + GC 파형(원본 시간축 그대로 보관)."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = ""                 # 원료명
    oil_type: str = ""             # 유종 (예: 등유, 용제, 윤활기유 등)
    sample_no: str = ""            # 시료번호 (엑셀 일괄가져오기 시 "의뢰번호"가 여기 들어감)
    marker_conc: float = 0.0       # 식별제 함량 (mg/L) - 배합/역추적 계산에 실제로 쓰이는 대표값
    density: float = 0.0           # 밀도 15C (g/cm3)
    viscosity: float = 0.0         # 동점도 40C (mm2/s)
    notes: str = ""                # 기타 시험값/비고
    source_filename: str = ""      # 원본 크로마토그램 파일명 (참고용)
    time: List[float] = field(default_factory=list)
    intensity: List[float] = field(default_factory=list)

    # -- 상세 시험값 (참고/표시용 - 배합 계산식에는 직접 쓰이지 않음) -----------
    collected_date: str = ""       # 채취일
    inspection_type: str = ""      # 검사유형
    flash_point: float = 0.0       # 인화점 (KS M 2010, TAG)
    distill_ibp: float = 0.0       # 증류성상 초류점
    distill_10: float = 0.0        # 증류성상 10% 유출온도
    distill_50: float = 0.0        # 증류성상 50% 유출온도
    distill_90: float = 0.0        # 증류성상 90% 유출온도
    distill_ep: float = 0.0        # 증류성상 종말점
    distill_residue: float = 0.0   # 증류성상 잔류량
    sulfur: float = 0.0            # 황분
    marker_1494db: float = 0.0     # 식별제 첨가량 - Unimark 1494DB
    marker_s10: float = 0.0        # 식별제 첨가량 - Accutrace S10
    composition_1: float = 0.0     # 조성분포 #1 (%)
    composition_2: float = 0.0     # 조성분포 #2 (%)
    composition_3: float = 0.0     # 조성분포 #3 (%, 없는 경우 0)

    @property
    def properties(self) -> FuelProperties:
        return FuelProperties(marker_conc=self.marker_conc, density=self.density, viscosity=self.viscosity)

    def has_waveform(self) -> bool:
        return len(self.time) >= 2 and len(self.time) == len(self.intensity)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "RawMaterialRecord":
        allowed = set(RawMaterialRecord.__dataclass_fields__.keys())
        clean = {k: v for k, v in d.items() if k in allowed}
        if "density" in clean:
            # 우리 자신이 저장한 파일은 항상 g/cm3라 정규화해도 값이 그대로지만,
            # 외부에서 가져온 JSON DB 파일(다른 단위로 기록된)에 대비한 안전장치.
            clean["density"] = normalize_density_g_cm3(clean["density"])
        return RawMaterialRecord(**clean)


class RawMaterialDatabase:
    """원료 DB 파일(JSON) 로드/저장 및 추가·편집·삭제(CRUD)를 담당."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self.records: List[RawMaterialRecord] = []

    # -- 영속화 ------------------------------------------------------------
    def load(self) -> None:
        if not os.path.exists(self.db_path):
            self.records = []
            return
        try:
            with open(self.db_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.records = [RawMaterialRecord.from_dict(item) for item in data]
        except (json.JSONDecodeError, OSError):
            self.records = []

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with open(self.db_path, "w", encoding="utf-8") as f:
            json.dump([r.to_dict() for r in self.records], f, ensure_ascii=False, indent=2)

    # -- CRUD ---------------------------------------------------------------
    # add/update/delete는 모두 "메모리 변경 + 저장"이 함께 성공하거나, 저장이
    # 실패하면 메모리도 원래 상태로 되돌린다. 그렇지 않으면 저장 실패 후에도
    # self.records만 앞서가서, 다음 성공적인 저장 때 사용자가 모르는 사이에
    # 실패했던 변경 내용까지 함께 디스크에 쓰여버리는 등 상태가 어긋난다.
    def add(self, record: RawMaterialRecord) -> None:
        self.records.append(record)
        try:
            self.save()
        except OSError:
            self.records.pop()
            raise

    def update(self, record: RawMaterialRecord) -> None:
        for i, r in enumerate(self.records):
            if r.id == record.id:
                previous = self.records[i]
                self.records[i] = record
                try:
                    self.save()
                except OSError:
                    self.records[i] = previous
                    raise
                return
        raise KeyError(f"레코드를 찾을 수 없습니다: {record.id}")

    def delete(self, record_id: str) -> None:
        previous = self.records
        self.records = [r for r in self.records if r.id != record_id]
        try:
            self.save()
        except OSError:
            self.records = previous
            raise

    def get(self, record_id: str) -> Optional[RawMaterialRecord]:
        for r in self.records:
            if r.id == record_id:
                return r
        return None

    def find_by_sample_no(self, sample_no: str) -> Optional[RawMaterialRecord]:
        """시료번호(엑셀 일괄가져오기의 '의뢰번호')로 기존 레코드를 찾는다."""
        for r in self.records:
            if r.sample_no == sample_no:
                return r
        return None

    def upsert(self, record: RawMaterialRecord) -> bool:
        """sample_no가 같은 기존 레코드가 있으면 그 id를 유지한 채 덮어쓰고(update),
        없으면 새로 추가(add)한다. 반환값: True면 새로 추가된 것, False면 기존 것을 갱신."""
        existing = self.find_by_sample_no(record.sample_no) if record.sample_no else None
        if existing is not None:
            record.id = existing.id
            self.update(record)
            return False
        self.add(record)
        return True

    # -- 가져오기/내보내기 ------------------------------------------------------
    def import_json(self, filepath: str) -> int:
        """다른 JSON DB 파일의 레코드들을 가져와 병합한다 (id 충돌 방지를 위해 새 id 부여).
        반환값: 추가된 건수."""
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        added = 0
        for item in data:
            rec = RawMaterialRecord.from_dict(item)
            rec.id = uuid.uuid4().hex[:12]
            self.records.append(rec)
            added += 1
        if added:
            self.save()
        return added

    def export_json(self, filepath: str) -> None:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump([r.to_dict() for r in self.records], f, ensure_ascii=False, indent=2)

    # -- 검색용 변환 ----------------------------------------------------------
    def to_candidates(self, reference_time: np.ndarray) -> List[RawMaterialCandidate]:
        """
        저장된 각 레코드의 원본 시간축 파형을, 현재 비교에 사용 중인 기준
        시간축(reference_time)으로 리샘플링·정규화하여 RawMaterialCandidate
        리스트로 변환한다.

        레코드마다 원본 GC run의 시간축·길이가 제각각일 수 있으므로, 배열 인덱스가
        아니라 실제 체류시간(time) 값을 기준으로 보간한다 — 이렇게 해야 비교 대상
        시료(diesel/fake)의 기준 시간축이 나중에 바뀌어도 정합성이 유지된다.
        """
        candidates: List[RawMaterialCandidate] = []
        for r in self.records:
            if not r.has_waveform():
                continue
            t = np.asarray(r.time, dtype=float)
            y = np.asarray(r.intensity, dtype=float)
            interp_func = interp1d(t, y, kind="linear", bounds_error=False, fill_value=0.0)
            resampled = np.nan_to_num(interp_func(reference_time), nan=0.0)
            max_val = resampled.max()
            normalized = resampled / max_val if max_val > 0 else resampled
            candidates.append(RawMaterialCandidate(
                name=self._display_name(r),
                intensity=normalized,
                properties=r.properties,
            ))
        return candidates

    @staticmethod
    def _display_name(r: "RawMaterialRecord") -> str:
        parts = [p for p in (r.oil_type, r.sample_no) if p]
        suffix = f" ({'/'.join(parts)})" if parts else ""
        return f"{r.name}{suffix}" if r.name else (r.sample_no or r.id)


def seed_example_records(db: RawMaterialDatabase) -> None:
    """DB 파일이 아직 한 번도 만들어지지 않은 최초 실행에서만 참고용 예시 원료
    4종을 시딩한다. 이름에 '[예시]'를 붙여 실제 데이터가 아님을 명시하며, DB
    관리 창에서 자유롭게 편집/삭제할 수 있다.

    '레코드가 비어 있으면 시딩'이 아니라 '파일이 없으면 시딩'으로 판단해야 한다.
    전자로 하면, 사용자가 예시 4건을 전부 삭제해 의도적으로 빈 DB를 만들어도
    다음 실행 때마다 예시가 되살아나 삭제 기능이 무의미해진다.
    """
    if os.path.exists(db.db_path) or db.records:
        return
    t = np.linspace(0.0, 30.0, 1500)

    def gauss(center_ratio, width, amp):
        center = t.min() + center_ratio * (t.max() - t.min())
        return amp * np.exp(-0.5 * ((t - center) / width) ** 2)

    examples = [
        ("[예시] 등유 유사 원료 A", "등유", "EX-001", 5.0, 0.800, 1.5,
         gauss(0.2, 0.8, 1.0) + gauss(0.5, 1.0, 0.3)),
        ("[예시] 용제 유사 원료 B", "용제", "EX-002", 2.0, 0.780, 1.1,
         gauss(0.35, 1.0, 1.0)),
        ("[예시] 윤활기유 유사 원료 C", "윤활기유", "EX-003", 1.0, 0.870, 8.0,
         gauss(0.7, 1.5, 1.0) + gauss(0.9, 1.0, 0.5)),
        ("[예시] 혼합 용제 원료 D", "혼합용제", "EX-004", 3.0, 0.820, 2.5,
         gauss(0.4, 0.6, 0.7) + gauss(0.6, 0.7, 0.7)),
    ]
    for name, oil_type, sample_no, marker, density, visc, wave in examples:
        db.records.append(RawMaterialRecord(
            name=name, oil_type=oil_type, sample_no=sample_no,
            marker_conc=marker, density=density, viscosity=visc,
            notes="최초 실행 시 자동 생성된 예시 데이터입니다. 실제 원료로 교체하거나 편집/삭제 하세요.",
            source_filename="(예시 데이터)",
            time=t.tolist(), intensity=wave.tolist(),
        ))
    db.save()


# ---------------------------------------------------------------------------
# 엑셀 마스터 목록 일괄 가져오기 (한국석유관리원 검사결과 형식)
# ---------------------------------------------------------------------------
# 컬럼은 헤더 문구가 아니라 위치(0-based)로 읽는다. 실제 엑셀 헤더 문구는
# "KS M  ISO 2160"처럼 공백이 들쭉날쭉하거나 버전마다 미묘하게 달라질 수 있어,
# 위치 매칭이 훨씬 안정적이다. 기대하는 배치 (Excel 열문자 -> 0-based 인덱스):
#   A(0)=의뢰번호, C(2)=채취일, D(3)=검사유형, E(4)=상표명, G(6)=제품명,
#   N(13)=인화점, O(14)=동점도,
#   P~U(15~20)=증류성상(초류점/10%/50%/90%유출온도/종말점/잔류량),
#   AB(27)=황분, AI(34)=식별제(Unimark 1494DB), AJ(35)=식별제(Accutrace S10),
#   AK(36)=밀도(kg/m3 - g/cm3로 변환 필요), AL~AN(37~39)=조성분포 #1/#2/#3
_COL_REQUEST_NO = 0
_COL_COLLECTED_DATE = 2
_COL_INSPECTION_TYPE = 3
_COL_BRAND = 4
_COL_PRODUCT_NAME = 6
_COL_FLASH_POINT = 13
_COL_VISCOSITY = 14
_COL_DISTILL_IBP = 15
_COL_DISTILL_10 = 16
_COL_DISTILL_50 = 17
_COL_DISTILL_90 = 18
_COL_DISTILL_EP = 19
_COL_DISTILL_RESIDUE = 20
_COL_SULFUR = 27
_COL_MARKER_1494DB = 34
_COL_MARKER_S10 = 35
_COL_DENSITY = 36
_COL_COMPOSITION_1 = 37
_COL_COMPOSITION_2 = 38
_COL_COMPOSITION_3 = 39
_MIN_REQUIRED_COLS = 40

CHROMATOGRAM_EXTENSIONS = (".csv", ".txt", ".tsv", ".xlsx", ".xls")


@dataclass
class ImportSummary:
    """엑셀 일괄 가져오기 결과 요약."""

    total_rows: int = 0
    added: int = 0
    updated: int = 0
    skipped: int = 0                                   # 의뢰번호가 비어 건너뛴 행
    matched_chromatogram: int = 0
    unmatched_chromatogram: List[str] = field(default_factory=list)  # 크로마토그램 못 찾은 의뢰번호 목록
    errors: List[str] = field(default_factory=list)


def _safe_float(value, default: float = 0.0) -> float:
    """엑셀 셀 값을 float로 안전 변환한다.
    NaN/빈 값은 default, "1.0 미만"처럼 텍스트가 섞인 값은 앞의 숫자만 추출한다
    (황분 컬럼처럼 검출한계 이하를 "X 미만"으로 표기하는 경우 대응)."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return default if pd.isna(value) else float(value)
    s = str(value).strip()
    if not s or s.lower() == "nan":
        return default
    m = re.search(r"[-+]?\d*\.?\d+", s)
    return float(m.group()) if m else default


def _safe_str(value, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, float) and pd.isna(value):
        return default
    s = str(value).strip()
    return default if s.lower() == "nan" else s


def _find_chromatogram_file(chromatogram_dir: str, sample_no: str) -> Optional[str]:
    """chromatogram_dir 안에서 sample_no(의뢰번호)에 해당하는 크로마토그램 파일을 찾는다.
    파일명이 의뢰번호와 정확히 같은 것(확장자 제외)을 우선 찾고, 없으면 의뢰번호를
    부분 문자열로 포함하는 파일명으로 한 번 더 찾는다 (예: "262102-01060_GC.csv")."""
    if not chromatogram_dir or not os.path.isdir(chromatogram_dir):
        return None
    entries = [f for f in os.listdir(chromatogram_dir)
               if os.path.splitext(f)[1].lower() in CHROMATOGRAM_EXTENSIONS]

    for f in entries:
        if os.path.splitext(f)[0] == sample_no:
            return os.path.join(chromatogram_dir, f)
    for f in entries:
        if sample_no and sample_no in os.path.splitext(f)[0]:
            return os.path.join(chromatogram_dir, f)
    return None


def _row_to_record(row, sample_no: str, marker_field: str) -> RawMaterialRecord:
    marker_1494db = _safe_float(row[_COL_MARKER_1494DB])
    marker_s10 = _safe_float(row[_COL_MARKER_S10])
    marker_conc = marker_1494db if marker_field == "1494db" else marker_s10

    brand = _safe_str(row[_COL_BRAND])
    product_name = _safe_str(row[_COL_PRODUCT_NAME])

    return RawMaterialRecord(
        name=brand or product_name or sample_no,
        oil_type=product_name,
        sample_no=sample_no,
        marker_conc=marker_conc,
        density=normalize_density_g_cm3(_safe_float(row[_COL_DENSITY])),  # g/cm3, kg/m3 자동판별
        viscosity=_safe_float(row[_COL_VISCOSITY]),
        collected_date=_safe_str(row[_COL_COLLECTED_DATE]),
        inspection_type=_safe_str(row[_COL_INSPECTION_TYPE]),
        flash_point=_safe_float(row[_COL_FLASH_POINT]),
        distill_ibp=_safe_float(row[_COL_DISTILL_IBP]),
        distill_10=_safe_float(row[_COL_DISTILL_10]),
        distill_50=_safe_float(row[_COL_DISTILL_50]),
        distill_90=_safe_float(row[_COL_DISTILL_90]),
        distill_ep=_safe_float(row[_COL_DISTILL_EP]),
        distill_residue=_safe_float(row[_COL_DISTILL_RESIDUE]),
        sulfur=_safe_float(row[_COL_SULFUR]),
        marker_1494db=marker_1494db,
        marker_s10=marker_s10,
        composition_1=_safe_float(row[_COL_COMPOSITION_1]),
        composition_2=_safe_float(row[_COL_COMPOSITION_2]),
        composition_3=_safe_float(row[_COL_COMPOSITION_3]),
    )


def import_master_excel(
    db: RawMaterialDatabase,
    excel_path: str,
    chromatogram_dir: Optional[str] = None,
    sheet_name=0,
    marker_field: str = "1494db",
) -> ImportSummary:
    """
    한국석유관리원 검사결과 마스터 엑셀(예: 등유 검사 목록)을 읽어 원료 후보
    DB에 일괄 반영한다.

    같은 의뢰번호(시료번호)가 이미 DB에 있으면 새로 추가하지 않고 값만
    갱신한다 (재실행해도 중복이 쌓이지 않도록). 크로마토그램 폴더 없이
    메타데이터만 다시 가져오는 경우, 기존에 이미 붙어 있던 크로마토그램은
    지우지 않고 유지한다.

    한 행 처리 중 오류가 나도 나머지 행 가져오기는 계속 진행하고, 오류
    내용은 summary.errors에 모아서 반환한다. 다만 마지막 저장(디스크 쓰기)
    자체가 실패하면 이번 가져오기로 인한 메모리상 변경 전체를 롤백한다
    (일부만 저장되고 일부는 안 된 상태로 남지 않도록).

    Parameters
    ----------
    chromatogram_dir : str, optional
        의뢰번호로 매칭할 크로마토그램 파일들이 들어있는 폴더. 지정하지 않으면
        각 레코드는 (기존 파형이 없다면) 파형 없이 생성되며, 나중에 DB
        관리창에 크로마토그램 파일을 드래그앤드롭하면 의뢰번호로 자동 매칭된다.
    marker_field : "1494db" | "s10"
        배합/역추적 계산에 실제로 사용할 대표 식별제 값으로 어느 컬럼을 쓸지
        선택한다 (두 마커 값 모두 참고용으로는 항상 함께 저장된다).
    """
    df = pd.read_excel(excel_path, sheet_name=sheet_name, header=None, skiprows=1)
    summary = ImportSummary(total_rows=len(df))

    if df.shape[1] <= _COL_COMPOSITION_3:
        summary.errors.append(
            f"엑셀 컬럼 수({df.shape[1]})가 예상보다 적습니다 (최소 {_MIN_REQUIRED_COLS}개 필요). "
            "시트/양식이 다른 파일일 수 있습니다."
        )
        return summary

    parser = GCDataParser()
    original_records = list(db.records)  # 마지막 저장이 실패할 경우 되돌리기 위한 스냅샷

    for _, row in df.iterrows():
        sample_no = _safe_str(row[_COL_REQUEST_NO])
        if not sample_no:
            summary.skipped += 1
            continue

        try:
            record = _row_to_record(row, sample_no, marker_field)
            existing = db.find_by_sample_no(sample_no)

            csv_path = _find_chromatogram_file(chromatogram_dir, sample_no) if chromatogram_dir else None
            if csv_path:
                wf = parser.load_file(csv_path)
                wf = parser.correct_baseline(wf)
                record.time = wf.time.tolist()
                record.intensity = wf.intensity.tolist()
                record.source_filename = os.path.basename(csv_path)
                summary.matched_chromatogram += 1
            elif chromatogram_dir:
                summary.unmatched_chromatogram.append(sample_no)

            if not record.has_waveform() and existing is not None and existing.has_waveform():
                record.time = existing.time
                record.intensity = existing.intensity
                record.source_filename = existing.source_filename

            if existing is not None:
                record.id = existing.id
                pos = next(i for i, r in enumerate(db.records) if r.id == existing.id)
                db.records[pos] = record
                summary.updated += 1
            else:
                db.records.append(record)
                summary.added += 1
        except Exception as e:
            summary.errors.append(f"[{sample_no}] {e}")

    try:
        db.save()
    except OSError as e:
        db.records = original_records
        summary.errors.append(f"저장 실패 - 이번 가져오기 전체가 취소되었습니다: {e}")

    return summary


# ---------------------------------------------------------------------------
# 모듈 단독 실행 시 스모크 테스트
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_db.json")
        db = RawMaterialDatabase(db_path)
        db.load()
        assert db.records == []

        seed_example_records(db)
        assert len(db.records) == 4
        assert os.path.exists(db_path)
        print(f"[OK] 예시 {len(db.records)}건 시딩 및 저장 완료")

        # 다른 인스턴스로 다시 로드해서 영속화 확인
        db2 = RawMaterialDatabase(db_path)
        db2.load()
        assert len(db2.records) == 4
        print("[OK] 재로드 시 레코드 유지 확인")

        # 편집
        rec = db2.records[0]
        rec.oil_type = "테스트유종"
        db2.update(rec)
        db3 = RawMaterialDatabase(db_path)
        db3.load()
        assert db3.get(rec.id).oil_type == "테스트유종"
        print("[OK] 편집(update) 및 영속화 확인")

        # 삭제
        db3.delete(rec.id)
        assert db3.get(rec.id) is None
        assert len(db3.records) == 3
        db4 = RawMaterialDatabase(db_path)
        db4.load()
        assert len(db4.records) == 3
        print("[OK] 삭제(delete) 및 영속화 확인")

        # 서로 다른 기준시간축으로 to_candidates() 정합성 확인
        # (원본 레코드 시간축은 0~30분인데, 비교 기준축을 5~20분으로 다르게 줘도
        #  실제 체류시간 값 기준으로 올바르게 보간되어야 한다 -> High 버그 재발 방지)
        ref_time_narrow = np.linspace(5.0, 20.0, 3000)
        candidates = db4.to_candidates(ref_time_narrow)
        assert len(candidates) == 3
        for c in candidates:
            assert c.intensity.shape == ref_time_narrow.shape
            assert c.intensity.max() <= 1.0 + 1e-9
        print(f"[OK] to_candidates(): {len(candidates)}건, 축 정합성/정규화 확인 (좁은 시간축 {ref_time_narrow.min()}~{ref_time_narrow.max()}분)")

        # 가져오기/내보내기
        export_path = os.path.join(tmpdir, "export.json")
        db4.export_json(export_path)
        db5 = RawMaterialDatabase(os.path.join(tmpdir, "empty.json"))
        db5.load()
        added = db5.import_json(export_path)
        assert added == 3
        assert len(db5.records) == 3
        print(f"[OK] 내보내기/가져오기 {added}건 확인")

        # ------------------------------------------------------------
        # 엑셀 마스터 목록 일괄 가져오기 (합성 데이터 - 실제 KPETRO 엑셀
        # 컬럼 배치를 그대로 재현. 사용자 개인 파일에 의존하지 않기 위해
        # 매번 새로 합성해서 테스트한다.)
        # ------------------------------------------------------------
        n_cols = 41
        row_a = [None] * n_cols
        row_a[0] = "TEST-001"       # 의뢰번호
        row_a[2] = "2026-01-01"     # 채취일
        row_a[3] = "의무검사"        # 검사유형
        row_a[4] = "SK에너지"        # 상표명
        row_a[6] = "등유"           # 제품명
        row_a[13] = 41.5            # 인화점
        row_a[14] = 1.182           # 동점도
        row_a[15], row_a[16], row_a[17], row_a[18], row_a[19], row_a[20] = (
            147.5, 163.6, 191.0, 241.8, 268.0, 1.2)  # 증류성상
        row_a[27] = "1.0 미만"       # 황분 (텍스트 섞인 값 - 파싱 테스트)
        row_a[34] = 15.4            # 식별제(1494DB)
        row_a[35] = 13.6            # 식별제(S10)
        row_a[36] = 792.6           # 밀도 (kg/m3)
        row_a[37], row_a[38], row_a[39] = 68.697, 31.303, 0.0  # 조성분포

        row_b = list(row_a)
        row_b[0] = "TEST-002"
        row_b[4] = "GS칼텍스"
        row_b[36] = 800.0

        excel_path = os.path.join(tmpdir, "master_test.xlsx")
        pd.DataFrame([row_a, row_b]).to_excel(excel_path, index=False)

        db6 = RawMaterialDatabase(os.path.join(tmpdir, "excel_import_db.json"))
        db6.load()
        summary = import_master_excel(db6, excel_path)
        assert summary.total_rows == 2
        assert summary.added == 2 and summary.updated == 0
        assert len(db6.records) == 2

        rec_a = db6.find_by_sample_no("TEST-001")
        assert rec_a is not None
        assert abs(rec_a.density - 0.7926) < 1e-9, "밀도 kg/m3->g/cm3 변환 오류"
        assert rec_a.sulfur == 1.0, "'X 미만' 텍스트 황분 파싱 오류"
        assert rec_a.marker_1494db == 15.4 and rec_a.marker_s10 == 13.6
        assert rec_a.marker_conc == 15.4, "기본 대표 식별제 값은 1494DB여야 함"
        print("[OK] 엑셀 일괄 가져오기: 밀도 단위변환/텍스트 황분 파싱/식별제 이중값 확인")

        # 크로마토그램 CSV를 나중에 별도로 매칭
        chromo_dir = os.path.join(tmpdir, "chromo")
        os.makedirs(chromo_dir)
        t = np.linspace(0, 25, 500)
        wave = np.exp(-0.5 * ((t - 10) / 1.5) ** 2)
        pd.DataFrame({"Retention Time (min)": t, "Intensity": wave}).to_csv(
            os.path.join(chromo_dir, "TEST-001.csv"), index=False)

        summary2 = import_master_excel(db6, excel_path, chromatogram_dir=chromo_dir)
        assert summary2.added == 0 and summary2.updated == 2, "재실행 시 upsert가 아니라 중복 추가됨"
        assert summary2.matched_chromatogram == 1
        assert summary2.unmatched_chromatogram == ["TEST-002"]
        assert len(db6.records) == 2, "재실행 후 레코드 수가 달라짐 (중복 생성 버그)"
        rec_a2 = db6.find_by_sample_no("TEST-001")
        assert rec_a2.has_waveform()
        print("[OK] 엑셀 재실행 시 중복 없이 upsert + 크로마토그램 의뢰번호 매칭 확인")

    print("\n[전체 통과] raw_material_db.py 스모크 테스트 완료.")
