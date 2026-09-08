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
3. 시료번호가 비어 있는 런(NV- 블랭크/세척 런 등)이나 신호 파일이 없는 폴더는 건너뛴다.
4. 같은 시료번호가 여러 .D 폴더에서 나오면(재주입 등) ChemStation 기록 시각이 더 늦은
   쪽을 최종 결과로 남긴다.
5. 결과를 "<시료번호>.csv" (헤더: "Retention Time (min),Intensity")로 저장한다 - OilScope의
   data_parser.py가 그대로 자동 인식하는 2열 포맷이다.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

import rainbow as rb

LogCallback = Callable[[str, str], None]  # (level, message) -> None
StopCallback = Callable[[], bool]

LEVEL_OK = "ok"
LEVEL_SKIP = "skip"
LEVEL_ERROR = "error"
LEVEL_INFO = "info"


@dataclass
class ConvertResult:
    converted: int = 0
    skipped_existing: int = 0
    skipped_no_sample: int = 0
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


def convert_all(
    root: str,
    out_dir: str,
    overwrite: bool = False,
    log_cb: Optional[LogCallback] = None,
    should_stop: Optional[StopCallback] = None,
) -> ConvertResult:
    """
    root 아래 모든 .D 폴더를 찾아 out_dir에 "<시료번호>.csv"로 변환한다.

    같은 시료번호가 여러 .D 폴더에서 나오면(재주입 등) ChemStation 기록 시각이 더
    늦은 쪽을 최종 결과로 남긴다. overwrite=False(기본)이면 out_dir에 이미 있는
    파일은 다시 만들지 않고 건너뛴다 - 매번 DATA 폴더 전체가 아니라 그 사이 새로
    쌓인 시료만 빠르게 처리하기 위함이다.
    """
    result = ConvertResult()
    os.makedirs(out_dir, exist_ok=True)

    def log(level: str, message: str) -> None:
        if log_cb:
            log_cb(level, message)

    d_folders = list(find_d_folders(root))
    log(LEVEL_INFO, f"{len(d_folders)}개의 .D 폴더를 찾았습니다.")

    # 시료번호별로 가장 최신 런만 남긴다 (재주입 등으로 중복될 수 있음).
    best: dict = {}
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

        acq_dt = meta.get("acq_datetime", datetime.min)
        prev = best.get(sample)
        if prev is None or acq_dt >= prev[3]:
            best[sample] = (d_folder, time, intensity, acq_dt)

    for sample, (d_folder, time, intensity, _acq_dt) in best.items():
        if should_stop and should_stop():
            log(LEVEL_INFO, "사용자 요청으로 중단했습니다.")
            break
        out_path = os.path.join(out_dir, f"{sample}.csv")
        if os.path.exists(out_path) and not overwrite:
            result.skipped_existing += 1
            log(LEVEL_SKIP, f"{sample}.csv: 이미 존재 - 건너뜀")
            continue
        try:
            write_csv(out_path, time, intensity)
            result.converted += 1
            log(LEVEL_OK, f"{sample}.csv 저장 완료 ({len(time)}개 포인트) <- {os.path.basename(d_folder)}")
        except Exception as e:  # noqa: BLE001
            result.failed += 1
            log(LEVEL_ERROR, f"{sample}.csv: 저장 실패 - {e}")

    log(
        LEVEL_INFO,
        f"완료: 변환 {result.converted}건 / 건너뜀 {result.skipped_existing + result.skipped_no_sample}건"
        f" / 실패 {result.failed}건",
    )
    return result
