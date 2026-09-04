# -*- coding: utf-8 -*-
"""
data_parser.py

Agilent 7890A ChemStation 엑셀 추출 GC 크로마토그램(Retention Time vs Intensity) 데이터를
읽어들여 분석 파이프라인에서 바로 사용할 수 있도록 가공하는 모듈.

주요 기능
---------
1. ChemStation 엑셀(.xlsx/.xls/.csv) 파일 로드 및 컬럼 자동 탐지/보정
2. 공통 기준 시간축(reference time axis) 생성
3. scipy.interpolate.interp1d 기반 리샘플링 (서로 다른 GC run 간 시간축 정합)
4. 베이스라인(baseline) 보정
5. Peak 정규화 (예: 최대 강도 1.0 기준 정규화)

클래스
------
GCDataParser : 위 기능을 캡슐화한 파서 클래스
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from scipy.interpolate import interp1d
from scipy.signal import savgol_filter


# ---------------------------------------------------------------------------
# 데이터 컨테이너
# ---------------------------------------------------------------------------
@dataclass
class GCWaveform:
    """단일 GC 크로마토그램 파형 데이터를 담는 컨테이너."""

    name: str
    time: np.ndarray                 # Retention Time (분)
    intensity: np.ndarray            # Intensity (raw)
    source_path: Optional[str] = None
    meta: dict = field(default_factory=dict)

    def copy(self) -> "GCWaveform":
        return GCWaveform(
            name=self.name,
            time=self.time.copy(),
            intensity=self.intensity.copy(),
            source_path=self.source_path,
            meta=dict(self.meta),
        )


# ---------------------------------------------------------------------------
# 메인 파서 클래스
# ---------------------------------------------------------------------------
class GCDataParser:
    """
    Agilent ChemStation 엑셀 파형 데이터 로딩 및 전처리 클래스.

    Parameters
    ----------
    time_col_candidates : Sequence[str]
        시간(Retention Time) 컬럼명으로 인식할 후보 문자열 목록 (대소문자 무시, 부분일치).
    intensity_col_candidates : Sequence[str]
        강도(Intensity) 컬럼명으로 인식할 후보 문자열 목록.
    ref_time_start, ref_time_end, ref_time_points : float, float, int
        공통 기준 시간축 생성 파라미터 (분 단위).
    """

    DEFAULT_TIME_CANDIDATES = (
        "retention time", "ret.time", "rt", "time", "min", "시간",
    )
    DEFAULT_INTENSITY_CANDIDATES = (
        "intensity", "signal", "response", "abundance", "value", "강도", "신호",
    )

    def __init__(
        self,
        time_col_candidates: Sequence[str] = DEFAULT_TIME_CANDIDATES,
        intensity_col_candidates: Sequence[str] = DEFAULT_INTENSITY_CANDIDATES,
        ref_time_start: float = 0.0,
        ref_time_end: float = 30.0,
        ref_time_points: int = 6000,
    ) -> None:
        self.time_col_candidates = [c.lower() for c in time_col_candidates]
        self.intensity_col_candidates = [c.lower() for c in intensity_col_candidates]
        self.ref_time_start = ref_time_start
        self.ref_time_end = ref_time_end
        self.ref_time_points = ref_time_points
        self._reference_time: Optional[np.ndarray] = None

    # ------------------------------------------------------------------
    # 기준 시간축
    # ------------------------------------------------------------------
    @property
    def reference_time(self) -> np.ndarray:
        """공통 기준 시간축 (lazy 생성, 캐시)."""
        if self._reference_time is None:
            self._reference_time = np.linspace(
                self.ref_time_start, self.ref_time_end, self.ref_time_points
            )
        return self._reference_time

    def set_reference_time_from_data(
        self,
        waveforms: Sequence[GCWaveform],
        auto_trim: bool = True,
        intensity_threshold_ratio: float = 0.005,
        min_duration_min: float = 0.5,
    ) -> np.ndarray:
        """
        여러 GCWaveform의 실제 시간 범위를 참고하여 공통 기준 시간축을 재설정한다.

        - 기본 동작: 모든 샘플이 겹치는 구간(최대 시작점 ~ 최소 종료점)을 사용해
          외삽(extrapolation)을 방지한다.
        - auto_trim=True: 각 샘플의 "시험 종료 시점"(유의미한 강도가 사라지는 시점)을
          자동 감지하여, 노이즈만 남은 꼬리 구간을 잘라낸 공통 구간을 사용한다.
          CSV마다 데이터 포인트 수(길이)가 달라도 서로 매칭된다.

        Parameters
        ----------
        waveforms : Sequence[GCWaveform]
            로드된 원시 파형들
        auto_trim : bool
            True면 시험 종료 시점을 자동 감지해 공통 종료시점을 더 보수적으로 잡는다.
        intensity_threshold_ratio : float
            '시험 종료'로 간주할 강도 임계값 (각 파형 최대강도 대비 비율)
        min_duration_min : float
            자동 트림 시 보장할 최소 구간 길이 (분)
        """
        valid = [wf for wf in waveforms if len(wf.time) > 0]
        if not valid:
            return self.reference_time

        starts = [wf.time.min() for wf in valid]
        common_start = max(starts)

        if auto_trim:
            ends = [
                self._detect_run_end_time(wf, intensity_threshold_ratio)
                for wf in valid
            ]
        else:
            ends = [wf.time.max() for wf in valid]
        common_end = min(ends)

        # 안전장치: 구간이 너무 짧거나 역전되면 자동트림을 포기하고 단순 겹침 구간 사용
        if auto_trim and (common_end - common_start) < min_duration_min:
            ends = [wf.time.max() for wf in valid]
            common_end = min(ends)

        if common_end <= common_start:
            # 겹치는 구간이 아예 없으면 기존 설정 유지
            return self.reference_time

        self.ref_time_start = float(common_start)
        self.ref_time_end = float(common_end)
        self._reference_time = np.linspace(
            self.ref_time_start, self.ref_time_end, self.ref_time_points
        )
        return self._reference_time

    @staticmethod
    def _detect_run_end_time(wf: GCWaveform, threshold_ratio: float = 0.005) -> float:
        """
        파형에서 '시험 종료 시점'을 추정한다: 최대 강도의 threshold_ratio 이하로
        내려가 이후로는 유의미한 피크가 없는 지점. 없으면 원래 종료시점을 그대로 반환.
        """
        t = wf.time
        y = wf.intensity
        if len(t) == 0:
            return 0.0
        max_val = y.max()
        if max_val <= 0:
            return float(t.max())

        threshold = max_val * threshold_ratio
        significant_idx = np.where(y > threshold)[0]
        if len(significant_idx) == 0:
            return float(t.max())

        last_sig_t = t[significant_idx[-1]]
        # 마지막 유의미 지점 이후로 아주 소량의 여유(꼬리)를 남긴다
        tail = max((t.max() - t.min()) * 0.005, (t[1] - t[0]) if len(t) > 1 else 0.0)
        end_t = min(last_sig_t + tail, float(t.max()))
        return float(end_t)

    # ------------------------------------------------------------------
    # 파일 로딩
    # ------------------------------------------------------------------
    @staticmethod
    def _detect_encoding(filepath: str) -> str:
        """
        바이트 순서표시(BOM)로 텍스트 파일 인코딩을 탐지한다.
        ChemStation 추출 CSV는 UTF-16 LE(BOM FF FE)를 사용하는 경우가 많다.
        """
        with open(filepath, "rb") as f:
            head = f.read(4)
        if head[:2] == b"\xff\xfe":
            return "utf-16-le"
        if head[:2] == b"\xfe\xff":
            return "utf-16-be"
        if head[:3] == b"\xef\xbb\xbf":
            return "utf-8-sig"
        return "utf-8"

    @staticmethod
    def _detect_separator(sample_line: str) -> str:
        """데이터 첫 줄을 보고 구분자를 추정한다 (탭 > 쉼표 > 공백 순으로 우선)."""
        if "\t" in sample_line:
            return "\t"
        if "," in sample_line:
            return ","
        return r"\s+"

    def _load_csv_with_autodetect(self, filepath: str) -> pd.DataFrame:
        """
        인코딩/구분자/헤더유무를 자동 탐지하여 CSV/TSV 파일을 DataFrame으로 로드한다.

        헤더가 없으면 첫 두 열을 'time', 'intensity'로 간주한다 (ChemStation
        등경유분 GC 조성분포 추출 파일 형식).
        """
        encoding = self._detect_encoding(filepath)

        # 1) 헤더 유무를 판단하기 위해 첫 몇 줄을 읽어 숫자 여부를 확인
        with open(filepath, "r", encoding=encoding) as f:
            first_lines = []
            for _ in range(5):
                line = f.readline()
                if not line:
                    break
                stripped = line.strip()
                if stripped:
                    first_lines.append(stripped)

        if not first_lines:
            raise ValueError(f"파일이 비어 있습니다: {filepath}")

        sep = self._detect_separator(first_lines[0])

        def try_parse_number(s: str) -> bool:
            try:
                float(s)
                return True
            except ValueError:
                return False

        # 정규식 구분자(\s+)는 str.split이 아닌 re.split으로 토큰화해야 정확하다
        if sep == r"\s+":
            import re
            first_tokens = re.split(sep, first_lines[0].strip())
        else:
            first_tokens = first_lines[0].split(sep)
        has_header = not all(try_parse_number(tok) for tok in first_tokens if tok != "")

        if has_header:
            df = pd.read_csv(filepath, encoding=encoding, sep=sep, engine="python")
        else:
            df = pd.read_csv(
                filepath, encoding=encoding, sep=sep, engine="python",
                header=None,
            )
            df = df.iloc[:, :2]
            df.columns = ["time", "intensity"]

        return df

    def load_file(self, filepath: str, name: Optional[str] = None,
                   sheet_name=0) -> GCWaveform:
        """
        ChemStation에서 추출한 엑셀(.xlsx/.xls) 또는 CSV 파일을 로드하여
        GCWaveform 객체로 변환한다.

        CSV는 아래 포맷들을 자동으로 처리한다:
            - 헤더 없는 2열 탭구분 (예: 등경유분 GC 조성분포 추출 파일)
            - 헤더 있는 쉼표/탭 구분
            - UTF-16/UTF-8 등 다양한 인코딩 (BOM 기반 자동 탐지)

        Parameters
        ----------
        filepath : str
            엑셀/CSV 파일 경로
        name : str, optional
            파형에 부여할 이름 (미지정 시 파일명 사용)
        sheet_name : int or str
            엑셀 시트 지정 (기본: 첫 번째 시트)
        """
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"파일을 찾을 수 없습니다: {filepath}")

        ext = os.path.splitext(filepath)[1].lower()
        if ext in (".xlsx", ".xls"):
            df = pd.read_excel(filepath, sheet_name=sheet_name)
        elif ext in (".csv", ".txt", ".tsv"):
            df = self._load_csv_with_autodetect(filepath)
        else:
            raise ValueError(f"지원하지 않는 파일 형식입니다: {ext}")

        time_col, intensity_col = self._detect_columns(df)
        raw_time = pd.to_numeric(df[time_col], errors="coerce").to_numpy(dtype=float)
        raw_intensity = pd.to_numeric(df[intensity_col], errors="coerce").to_numpy(dtype=float)

        # NaN 제거 및 시간 오름차순 정렬
        mask = ~(np.isnan(raw_time) | np.isnan(raw_intensity))
        raw_time = raw_time[mask]
        raw_intensity = raw_intensity[mask]
        order = np.argsort(raw_time)
        raw_time = raw_time[order]
        raw_intensity = raw_intensity[order]

        # 중복 시간값 제거 (동일 시간에 여러 값이 있는 경우 평균 처리)
        raw_time, raw_intensity = self._deduplicate_time(raw_time, raw_intensity)

        wf_name = name if name is not None else os.path.splitext(os.path.basename(filepath))[0]
        return GCWaveform(
            name=wf_name,
            time=raw_time,
            intensity=raw_intensity,
            source_path=filepath,
            meta={"time_col": time_col, "intensity_col": intensity_col},
        )

    def _detect_columns(self, df: pd.DataFrame) -> Tuple[str, str]:
        """컬럼명 후보 목록을 이용해 시간/강도 컬럼을 자동 탐지한다."""
        columns_lower = {c: str(c).lower().strip() for c in df.columns}

        def find_match(candidates: Sequence[str]) -> Optional[str]:
            for col, low in columns_lower.items():
                for cand in candidates:
                    if cand in low:
                        return col
            return None

        time_col = find_match(self.time_col_candidates)
        intensity_col = find_match(self.intensity_col_candidates)

        # fallback: 못 찾으면 숫자형 컬럼 중 처음 두 개를 사용
        if time_col is None or intensity_col is None:
            numeric_cols = [
                c for c in df.columns
                if pd.api.types.is_numeric_dtype(df[c])
            ]
            if len(numeric_cols) >= 2:
                if time_col is None:
                    time_col = numeric_cols[0]
                if intensity_col is None:
                    intensity_col = numeric_cols[1] if numeric_cols[1] != time_col else numeric_cols[0]

        if time_col is None or intensity_col is None:
            raise ValueError(
                "시간(Retention Time) 또는 강도(Intensity) 컬럼을 자동으로 인식하지 못했습니다. "
                f"컬럼 목록: {list(df.columns)}"
            )
        return time_col, intensity_col

    @staticmethod
    def _deduplicate_time(time_arr: np.ndarray, intensity_arr: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """동일한 시간 값이 중복될 경우 강도값을 평균내어 단조증가 배열로 만든다."""
        if len(time_arr) == 0:
            return time_arr, intensity_arr
        df = pd.DataFrame({"t": time_arr, "i": intensity_arr})
        df = df.groupby("t", as_index=False).mean()
        return df["t"].to_numpy(dtype=float), df["i"].to_numpy(dtype=float)

    # ------------------------------------------------------------------
    # 리샘플링 (interp1d)
    # ------------------------------------------------------------------
    def resample(
        self,
        waveform: GCWaveform,
        target_time: Optional[np.ndarray] = None,
        kind: str = "linear",
        fill_value: float = 0.0,
    ) -> GCWaveform:
        """
        scipy.interpolate.interp1d를 이용하여 파형을 공통 기준 시간축(target_time)으로
        리샘플링한다. 지정하지 않으면 self.reference_time을 사용한다.
        """
        if target_time is None:
            target_time = self.reference_time

        if len(waveform.time) < 2:
            raise ValueError(f"'{waveform.name}' 파형의 데이터 포인트가 부족합니다 (리샘플링 불가).")

        interp_func = interp1d(
            waveform.time,
            waveform.intensity,
            kind=kind,
            bounds_error=False,
            fill_value=fill_value,
        )
        resampled_intensity = interp_func(target_time)
        resampled_intensity = np.nan_to_num(resampled_intensity, nan=fill_value)

        result = waveform.copy()
        result.time = target_time.copy()
        result.intensity = resampled_intensity
        result.meta["resampled"] = True
        return result

    # ------------------------------------------------------------------
    # 베이스라인 보정
    # ------------------------------------------------------------------
    def correct_baseline(
        self,
        waveform: GCWaveform,
        method: str = "percentile",
        percentile: float = 5.0,
        window_length: int = 101,
        polyorder: int = 3,
    ) -> GCWaveform:
        """
        파형의 베이스라인(기저선)을 보정한다.

        method:
            "percentile" - 하위 percentile 값을 베이스라인으로 간주하고 차감 (기본, 빠르고 안정적)
            "savgol"     - Savitzky-Golay 필터로 추정한 저주파 성분을 베이스라인으로 차감
            "min"        - 전체 최소값을 차감 (단순)
        """
        result = waveform.copy()
        intensity = result.intensity

        if method == "percentile":
            baseline_value = np.percentile(intensity, percentile)
            corrected = intensity - baseline_value
        elif method == "savgol":
            wl = window_length
            if wl >= len(intensity):
                wl = len(intensity) - 1 if len(intensity) % 2 == 0 else len(intensity)
                wl = max(wl, polyorder + 2)
            if wl % 2 == 0:
                wl += 1
            wl = max(wl, polyorder + 2)
            baseline = savgol_filter(intensity, window_length=min(wl, len(intensity) - (1 - len(intensity) % 2)),
                                      polyorder=polyorder, mode="interp")
            corrected = intensity - baseline
        elif method == "min":
            corrected = intensity - intensity.min()
        else:
            raise ValueError(f"알 수 없는 베이스라인 보정 방식입니다: {method}")

        corrected[corrected < 0] = 0.0
        result.intensity = corrected
        result.meta["baseline_corrected"] = method
        return result

    # ------------------------------------------------------------------
    # 정규화
    # ------------------------------------------------------------------
    def normalize(
        self,
        waveform: GCWaveform,
        method: str = "max",
        target_max: float = 1.0,
    ) -> GCWaveform:
        """
        Peak 정규화를 수행한다.

        method:
            "max"  - 최대 강도를 target_max로 정규화 (기본)
            "area" - 전체 면적(적분값)이 target_max가 되도록 정규화
            "zscore" - 평균 0, 표준편차 1로 표준화 (형태 비교용, 음수 포함될 수 있음)
        """
        result = waveform.copy()
        intensity = result.intensity

        if method == "max":
            max_val = np.max(intensity)
            if max_val <= 0:
                normalized = np.zeros_like(intensity)
            else:
                normalized = intensity / max_val * target_max
        elif method == "area":
            # numpy 2.x에서는 np.trapz가 제거되어 np.trapezoid를 사용
            area = np.trapezoid(intensity, result.time) if hasattr(np, "trapezoid") else np.trapz(intensity, result.time)
            if area <= 0:
                normalized = np.zeros_like(intensity)
            else:
                normalized = intensity / area * target_max
        elif method == "zscore":
            mean = intensity.mean()
            std = intensity.std()
            normalized = (intensity - mean) / std if std > 0 else intensity - mean
        else:
            raise ValueError(f"알 수 없는 정규화 방식입니다: {method}")

        result.intensity = normalized
        result.meta["normalized"] = method
        return result

    # ------------------------------------------------------------------
    # 전체 파이프라인 (편의 메서드)
    # ------------------------------------------------------------------
    def process(
        self,
        filepath: str,
        name: Optional[str] = None,
        baseline_method: str = "percentile",
        normalize_method: str = "max",
        do_resample: bool = True,
        fit_reference_to_data: bool = True,
    ) -> GCWaveform:
        """
        로드 -> 베이스라인 보정 -> 리샘플링 -> 정규화 순서로 전체 전처리를 수행하는
        편의 메서드.

        fit_reference_to_data=True 이면, 리샘플링 전에 해당 파일의 실제 시간범위로
        기준 시간축을 자동 재설정한다 (외삽으로 인한 0 채움 방지). 여러 파일을
        비교해야 하는 경우에는 `set_reference_time_from_data()`를 먼저 호출하고
        이 파라미터를 False로 두는 것이 좋다.
        """
        wf = self.load_file(filepath, name=name)
        wf = self.correct_baseline(wf, method=baseline_method)
        if do_resample:
            if fit_reference_to_data:
                self.set_reference_time_from_data([wf])
            wf = self.resample(wf)
        wf = self.normalize(wf, method=normalize_method)
        return wf


# ---------------------------------------------------------------------------
# 모듈 단독 실행 시 간단한 자체 테스트 (샘플 데이터 생성 후 파이프라인 확인)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # 가상의 GC 데이터로 파이프라인 동작 확인 (실제 파일 없이 스모크 테스트)
    rng = np.random.default_rng(42)
    t = np.linspace(0, 25, 500)
    signal = (
        3.0 * np.exp(-0.5 * ((t - 5) / 0.3) ** 2)
        + 5.0 * np.exp(-0.5 * ((t - 12) / 0.5) ** 2)
        + 0.2 * rng.standard_normal(len(t))
        + 0.5  # baseline offset
    )
    df_test = pd.DataFrame({"Retention Time (min)": t, "Intensity": signal})
    tmp_path = os.path.join(os.path.dirname(__file__), "_smoke_test_gc.csv")
    df_test.to_csv(tmp_path, index=False)

    parser = GCDataParser()
    waveform = parser.process(tmp_path, name="SmokeTest")
    print(f"[OK] '{waveform.name}' 처리 완료: {len(waveform.time)} 포인트, "
          f"max={waveform.intensity.max():.3f}, min={waveform.intensity.min():.3f}")

    os.remove(tmp_path)
