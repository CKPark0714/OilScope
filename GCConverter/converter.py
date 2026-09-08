# -*- coding: utf-8 -*-
"""
converter.py

ChemStation .D 폴더(FID 크로마토그램)를 일괄 스캔하여, OilScope가 바로 읽을 수 있는
"시료번호.csv" 파일들로 변환하는 핵심 로직 (GUI 없이 단독으로도 쓸 수 있다).

동작 개요
---------
1. 지정한 루트 폴더(예: C:\\Chem32\\1\\DATA) 아래를 재귀적으로 뒤져 모든 *.D 폴더를 찾는다.
2. 각 .D 폴더 안의 FID 신호 파일(FID1A.ch 등)을 rainbow-api로 읽어
   - 시료번호: ChemStation의 Notebook 필드 값 (=시료 등록 시 항상 입력하는 시료번호/의뢰번호)
   - Retention Time / Intensity 파형
   을 뽑아낸다. 이 필드는 SAMPLE.XML의 <Name>과 동일한 값이며, 검사자가 직접 켐스테이션
   "Export > CSV File"로 내보낼 때 File Name에 적어 넣던 바로 그 값이다.
3. 시료번호가 비어 있는 런(NV- 블랭크/세척 런 등), 신호 파일이 없는 폴더, 신호가 사실상
   없는 빈 런은 건너뛴다.
4. 같은 시료번호가 여러 .D 폴더에서 나오면(재주입 등) 모두 남기되, ChemStation 기록 시각이
   가장 늦은 런이 "<시료번호>.csv"를, 그보다 이전 런들이 오래된 순으로
   "<시료번호>-1.csv", "-2.csv" ...를 갖는다.
5. 결과를 "<시료번호>.csv" (헤더: "Retention Time (min),Intensity")로 저장한다 - OilScope의
   data_parser.py가 그대로 자동 인식하는 2열 포맷이다.
"""

from __future__ import annotations

import csv
import os
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, List, Optional

import numpy as np
import rainbow as rb

LogCallback = Callable[[str, str], None]  # (level, message) -> None
StopCallback = Callable[[], bool]

# 빈 런 판정 기준: 그 배치에서 시료번호가 확인된 런들의 신호 진폭 중앙값에 이 비율을
# 곱한 값보다 진폭이 작으면 "신호가 사실상 없다"고 본다. 장비/검출기마다 절대 수치가
# 크게 다르므로 고정 임계값 대신 그날 배치 자체를 기준으로 삼는다.
EMPTY_AMPLITUDE_RATIO = 0.01

LEVEL_OK = "ok"
LEVEL_SKIP = "skip"
LEVEL_ERROR = "error"
LEVEL_INFO = "info"


@dataclass
class ConvertResult:
    converted: int = 0
    skipped_existing: int = 0
    skipped_no_sample: int = 0
    skipped_empty: int = 0
    failed: int = 0


def _parse_acq_datetime(raw: Optional[str]) -> datetime:
    """metadata['date']는 보통 '07-Sep-26, 10:41:14' 형식이다. 파싱 실패 시 아주 과거로 취급."""
    if not raw:
        return datetime.min
    for fmt in ("%d-%b-%y, %H:%M:%S", "%d-%b-%Y, %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return datetime.min


def find_d_folders(root: str):
    """root 아래를 재귀적으로 뒤져 ChemStation 데이터 폴더(*.D)를 모두 찾는다."""
    for dirpath, dirnames, _filenames in os.walk(root):
        for name in list(dirnames):
            if name.upper().endswith(".D"):
                yield os.path.join(dirpath, name)


def read_signal(d_folder: str):
    """
    .D 폴더에서 FID 신호 하나를 읽는다.

    반환값: (시료번호 또는 None, time 배열, intensity 배열, meta dict)
    시료번호가 None이면 time/intensity는 None이고, meta["reason"]에 건너뛴 이유가 담긴다.
    """
    dx = rb.read(d_folder)
    if not dx.datafiles:
        return None, None, None, {"reason": "신호 파일 없음(.ch)"}

    # 여러 검출기 채널이 있을 경우 FID 채널을 우선한다.
    datafile = next(
        (f for f in dx.datafiles if f.name.upper().startswith("FID")),
        dx.datafiles[0],
    )

    sample = (datafile.metadata.get("notebook") or "").strip()
    if not sample:
        return None, None, None, {"reason": "시료번호(Notebook) 없음 - 블랭크/세척 런으로 추정"}

    time = datafile.xlabels
    intensity = datafile.data[:, 0]
    meta = dict(datafile.metadata)
    meta["acq_datetime"] = _parse_acq_datetime(meta.get("date"))
    return sample, time, intensity, meta


def write_csv(out_path: str, time, intensity) -> None:
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Retention Time (min)", "Intensity"])
        for t, y in zip(time, intensity):
            writer.writerow([f"{t:.6f}", f"{y:.6f}"])


@dataclass
class _Run:
    """1차 스캔에서 모으는 런 한 건의 요약 (파형 배열은 들고 있지 않는다)."""
    folder: str
    sample: str
    acq_dt: datetime
    amplitude: float


def _amplitude(intensity) -> float:
    """신호의 세기 폭(최대-최소). 빈 런을 가려내는 데 쓴다."""
    arr = np.asarray(intensity, dtype=float)
    if arr.size == 0:
        return 0.0
    return float(np.nanmax(arr) - np.nanmin(arr))


def convert_all(
    root: str,
    out_dir: str,
    overwrite: bool = False,
    log_cb: Optional[LogCallback] = None,
    should_stop: Optional[StopCallback] = None,
) -> ConvertResult:
    """
    root 아래 모든 .D 폴더를 찾아 out_dir에 "<시료번호>.csv"로 변환한다.

    같은 시료번호가 여러 .D 폴더에서 나오면(재주입 등) 어느 하나를 버리지 않고 전부
    남긴다. ChemStation 기록 시각이 가장 늦은 런이 "<시료번호>.csv"를 갖고, 그보다
    이전 런들이 오래된 순으로 "-1", "-2" 꼬리표를 받는다 - OilScope는 파일명으로
    시험 데이터와 매칭하므로 꼬리표 없는 파일이 그 시료의 대표 파형이 된다.

    신호가 사실상 없는 런(세척/블랭크성 런, 주입 실패 등)은 CSV로 만들지 않는다.
    판정 기준은 EMPTY_AMPLITUDE_RATIO 참고.

    overwrite=False(기본)이면 out_dir에 이미 있는 파일은 다시 만들지 않는다 - 매번
    DATA 폴더 전체가 아니라 그 사이 새로 쌓인 시료만 빠르게 처리하기 위함이다. 단
    같은 시료번호에 런이 둘 이상인 경우에는 예외로 항상 다시 쓴다. 새 재주입이
    들어오면 대표 파일과 꼬리표 번호가 통째로 밀리므로, 건너뛰면 예전 순서로 만든
    파일이 그대로 남아 내용과 이름이 어긋나기 때문이다.
    """
    result = ConvertResult()
    os.makedirs(out_dir, exist_ok=True)

    def log(level: str, message: str) -> None:
        if log_cb:
            log_cb(level, message)

    d_folders = list(find_d_folders(root))
    log(LEVEL_INFO, f"{len(d_folders)}개의 .D 폴더를 찾았습니다.")

    # 1차 스캔: 시료번호/기록시각/신호 진폭만 모은다. 파형 배열은 여기서 들고 있지
    # 않고 실제로 쓸 때 다시 읽는다 (DATA 폴더가 수천 건이어도 메모리가 늘지 않도록).
    runs: List[_Run] = []
    for d_folder in d_folders:
        if should_stop and should_stop():
            log(LEVEL_INFO, "사용자 요청으로 중단했습니다.")
            return result
        try:
            sample, time, intensity, meta = read_signal(d_folder)
        except Exception as e:  # noqa: BLE001 - 폴더 하나의 오류가 전체를 막지 않도록
            result.failed += 1
            log(LEVEL_ERROR, f"{os.path.basename(d_folder)}: 읽기 실패 - {e}")
            continue

        if sample is None:
            result.skipped_no_sample += 1
            log(LEVEL_SKIP, f"{os.path.basename(d_folder)}: {meta['reason']}")
            continue

        runs.append(_Run(
            folder=d_folder,
            sample=sample,
            acq_dt=meta.get("acq_datetime", datetime.min),
            amplitude=_amplitude(intensity),
        ))

    # 빈 런 걸러내기 - 기준은 이 배치 자체의 진폭 중앙값
    threshold = 0.0
    if runs:
        threshold = statistics.median(r.amplitude for r in runs) * EMPTY_AMPLITUDE_RATIO
        log(LEVEL_INFO, f"빈 데이터 판정 기준: 신호 진폭 {threshold:.4g} 미만"
                        f" (시료번호가 있는 런 진폭 중앙값의 {EMPTY_AMPLITUDE_RATIO:.0%})")

    valid: List[_Run] = []
    for r in runs:
        if r.amplitude <= 0.0 or r.amplitude < threshold:
            result.skipped_empty += 1
            log(LEVEL_SKIP, f"{r.sample} <- {os.path.basename(r.folder)}:"
                            f" 신호가 거의 없음(진폭 {r.amplitude:.4g}) - 빈 데이터로 건너뜀")
        else:
            valid.append(r)

    # 시료번호별로 묶어 파일명을 정한다 (최신 런이 꼬리표 없는 대표 파일).
    by_sample: dict = defaultdict(list)
    for r in valid:
        by_sample[r.sample].append(r)

    plan: List[tuple] = []  # (출력 파일명, 런, 같은 시료번호에 런이 여러 개인가)
    for sample, group in by_sample.items():
        group.sort(key=lambda r: (r.acq_dt, r.folder))
        multiple = len(group) > 1
        plan.append((f"{sample}.csv", group[-1], multiple))
        for n, r in enumerate(group[:-1], start=1):
            plan.append((f"{sample}-{n}.csv", r, multiple))

    for out_name, r, multiple in sorted(plan, key=lambda p: p[0]):
        if should_stop and should_stop():
            log(LEVEL_INFO, "사용자 요청으로 중단했습니다.")
            break
        out_path = os.path.join(out_dir, out_name)
        if os.path.exists(out_path) and not overwrite and not multiple:
            result.skipped_existing += 1
            log(LEVEL_SKIP, f"{out_name}: 이미 존재 - 건너뜀")
            continue
        try:
            _sample, time, intensity, _meta = read_signal(r.folder)
            write_csv(out_path, time, intensity)
            result.converted += 1
            log(LEVEL_OK, f"{out_name} 저장 완료 ({len(time)}개 포인트)"
                          f" <- {os.path.basename(r.folder)}")
        except Exception as e:  # noqa: BLE001
            result.failed += 1
            log(LEVEL_ERROR, f"{out_name}: 저장 실패 - {e}")

    skipped = result.skipped_existing + result.skipped_no_sample + result.skipped_empty
    log(
        LEVEL_INFO,
        f"완료: 변환 {result.converted}건 / 건너뜀 {skipped}건"
        f"(이미 있음 {result.skipped_existing} · 시료번호 없음 {result.skipped_no_sample}"
        f" · 빈 데이터 {result.skipped_empty})"
        f" / 실패 {result.failed}건",
    )
    return result
