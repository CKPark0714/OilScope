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
import uuid
from dataclasses import dataclass, field, asdict
from typing import List, Optional

import numpy as np
from scipy.interpolate import interp1d

from analyzer import FuelProperties, RawMaterialCandidate

DEFAULT_DB_PATH = os.path.join(os.path.expanduser("~"), ".fakeoilanalyzer", "raw_material_db.json")


@dataclass
class RawMaterialRecord:
    """원료 후보 1건: 식별 정보 + 물성치 + GC 파형(원본 시간축 그대로 보관)."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = ""                 # 원료명
    oil_type: str = ""             # 유종 (예: 등유, 용제, 윤활기유 등)
    sample_no: str = ""            # 시료번호
    marker_conc: float = 0.0       # 식별제 함량 (mg/L)
    density: float = 0.0           # 밀도 15C (g/cm3)
    viscosity: float = 0.0         # 동점도 40C (mm2/s)
    notes: str = ""                # 기타 시험값/비고
    source_filename: str = ""      # 원본 크로마토그램 파일명 (참고용)
    time: List[float] = field(default_factory=list)
    intensity: List[float] = field(default_factory=list)

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

    print("\n[전체 통과] raw_material_db.py 스모크 테스트 완료.")
