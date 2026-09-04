# -*- coding: utf-8 -*-
"""
analyzer.py

가짜석유(경유+미상 원료 혼합물) 분석 핵심 로직 모듈.

Case 1: 원료를 알고 있는 경우
    - 경유(Diesel) 및 원료(Raw material) 각각의 GC 파형/물성치가 주어졌을 때,
      실제 가짜석유(Fake) 시료를 가장 잘 재현하는 혼합비율 a (경유 부피비율, 0~1)를
      scipy.optimize.minimize(SLSQP)로 역추적한다.
    - 임의의 비율 a에 대해 예상 GC 파형과 물성치(밀도/동점도/식별제)를 계산하는
      배합 시뮬레이션 기능을 제공한다 (슬라이더 실시간 갱신용).

Case 2: 원료를 모르는 경우
    - 경유와 가짜석유 파형만 주어졌을 때, 혼합비율 a를 가정하고
      미지 원료의 파형을 역추정(Deconvolution)한다:
          R_est(t) = max(0, (Fake(t) - a*Diesel(t)) / (1 - a))
    - 역추정된 파형(및 물성치)을 원료 DB와 코사인 유사도로 비교하여 Top-N 후보를 추천한다.

물성 혼합 수식
--------------
- 식별제 함량 (mg/L): 선형 혼합       C_mix = a*C_diesel + (1-a)*C_raw
- 밀도 (15C, g/cm3):   선형 혼합       rho_mix = a*rho_diesel + (1-a)*rho_raw
- 동점도 (40C, mm2/s): 로그-선형 혼합  ln(nu_mix) = a*ln(nu_diesel) + (1-a)*ln(nu_raw)

여기서 a는 "경유"의 부피 비율 (0.0 ~ 1.0), (1-a)는 "원료"의 부피 비율이다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

import numpy as np
from scipy.optimize import minimize
from sklearn.metrics.pairwise import cosine_similarity


# ---------------------------------------------------------------------------
# 데이터 구조
# ---------------------------------------------------------------------------
@dataclass
class FuelProperties:
    """단일 시료(경유/원료/가짜석유 등)의 물성치."""

    marker_conc: float = 0.0     # 식별제 함량 (mg/L)
    density: float = 0.0         # 밀도 15C (g/cm3)
    viscosity: float = 0.0       # 동점도 40C (mm2/s)

    def as_array(self) -> np.ndarray:
        return np.array([self.marker_conc, self.density, self.viscosity], dtype=float)


@dataclass
class FuelSample:
    """GC 파형 + 물성치를 함께 담는 시료 컨테이너 (Case1/2 공용)."""

    name: str
    time: np.ndarray                     # 공통 기준 시간축 (모든 샘플이 동일해야 함)
    intensity: np.ndarray                # 정규화된 GC 강도
    properties: FuelProperties = field(default_factory=FuelProperties)


@dataclass
class RawMaterialCandidate:
    """원료 DB에 등록된 후보 원료 (Case 2 유사도 탐색용)."""

    name: str
    intensity: np.ndarray                # 기준 시간축에 맞춰 리샘플링/정규화된 파형
    properties: FuelProperties = field(default_factory=FuelProperties)


@dataclass
class MatchResult:
    """DB 유사도 탐색 결과 1건."""

    candidate_name: str
    similarity: float                    # 코사인 유사도 (0~1에 가까울수록 유사)
    property_distance: float             # 물성치 정규화 거리 (참고용)
    combined_score: float                # 파형 유사도 + 물성치를 종합한 최종 점수


@dataclass
class CandidateMatchResult:
    """DB 후보 1건을 실제 '원료'로 가정하고 SLSQP로 최적 혼합비율을 맞춘 결과."""

    candidate_name: str
    a_optimal: float                     # 이 후보를 원료로 가정했을 때 최적 경유 비율
    raw_ratio: float                     # 1 - a_optimal
    final_cost: float                    # MixRatioEstimator 목적함수 최종값 (작을수록 더 잘 맞음)
    success: bool                        # SLSQP 수렴 여부
    estimated_properties: FuelProperties
    wave_similarity: float               # 참고용 코사인 유사도 (최적 a에서의 예상 파형 vs 실측 가짜석유)
    candidate: "RawMaterialCandidate" = None   # 원본 후보 객체 (이름이 중복돼도 정확히 식별하기 위함)


# ---------------------------------------------------------------------------
# 물성 혼합 공식 (모듈 레벨 함수 - 재사용 목적)
# ---------------------------------------------------------------------------
def mix_marker_conc(a: float, c_diesel: float, c_raw: float) -> float:
    """식별제 함량 선형 혼합: C_mix = a*C_diesel + (1-a)*C_raw"""
    return a * c_diesel + (1.0 - a) * c_raw


def mix_density(a: float, rho_diesel: float, rho_raw: float) -> float:
    """밀도 선형 혼합: rho_mix = a*rho_diesel + (1-a)*rho_raw"""
    return a * rho_diesel + (1.0 - a) * rho_raw


def mix_viscosity(a: float, nu_diesel: float, nu_raw: float) -> float:
    """
    동점도 로그-선형 혼합: ln(nu_mix) = a*ln(nu_diesel) + (1-a)*ln(nu_raw)
    (동점도는 0보다 커야 하며, 0 이하 입력 시 아주 작은 양수로 대체하여 log 오류를 방지한다.)
    """
    eps = 1e-9
    nu_d = max(nu_diesel, eps)
    nu_r = max(nu_raw, eps)
    ln_mix = a * np.log(nu_d) + (1.0 - a) * np.log(nu_r)
    return float(np.exp(ln_mix))


def mix_waveform(a: float, diesel_intensity: np.ndarray, raw_intensity: np.ndarray) -> np.ndarray:
    """GC 파형 선형 혼합 (부피비 가정): I_mix(t) = a*I_diesel(t) + (1-a)*I_raw(t)"""
    return a * diesel_intensity + (1.0 - a) * raw_intensity


def mix_properties(a: float, diesel: FuelProperties, raw: FuelProperties) -> FuelProperties:
    """세 물성치를 한번에 혼합 계산."""
    return FuelProperties(
        marker_conc=mix_marker_conc(a, diesel.marker_conc, raw.marker_conc),
        density=mix_density(a, diesel.density, raw.density),
        viscosity=mix_viscosity(a, diesel.viscosity, raw.viscosity),
    )


# ---------------------------------------------------------------------------
# 배합 시뮬레이터 (슬라이더 연동용)
# ---------------------------------------------------------------------------
class FuelBlendingSimulator:
    """
    경유 + 원료 파형/물성치를 입력받아, 임의의 혼합 비율 a에 대한
    예상 GC 파형 및 물성치를 즉시 계산해주는 시뮬레이터.
    GUI 슬라이더 이벤트에서 반복 호출되는 것을 전제로 가볍게 설계되었다.
    """

    def __init__(self, diesel: FuelSample, raw: FuelSample):
        if diesel.intensity.shape != raw.intensity.shape:
            raise ValueError("경유와 원료의 파형 길이가 일치해야 합니다 (동일 기준시간축 리샘플링 필요).")
        self.diesel = diesel
        self.raw = raw

    def simulate(self, a: float) -> Tuple[np.ndarray, FuelProperties]:
        """
        a: 경유 부피비율 (0.0 ~ 1.0)
        반환: (예상 GC 강도 배열, 예상 물성치)
        """
        a = float(np.clip(a, 0.0, 1.0))
        est_waveform = mix_waveform(a, self.diesel.intensity, self.raw.intensity)
        est_props = mix_properties(a, self.diesel.properties, self.raw.properties)
        return est_waveform, est_props

    def simulate_series(self, a_values: Sequence[float]) -> List[Tuple[float, np.ndarray, FuelProperties]]:
        """여러 비율에 대해 일괄 시뮬레이션 (비교/스캔 용도)."""
        results = []
        for a in a_values:
            waveform, props = self.simulate(a)
            results.append((float(a), waveform, props))
        return results


# ---------------------------------------------------------------------------
# Case 1: 원료를 알고 있는 경우 - SLSQP 기반 혼합비율 역추적
# ---------------------------------------------------------------------------
class MixRatioEstimator:
    """
    실제 가짜석유(Fake) 시료의 GC 파형 및 물성치를 기준으로,
    경유:원료 혼합비율 a를 scipy.optimize.minimize(SLSQP)를 통해 추정한다.

    목적함수:
        J(a) = w_wave * ||Fake - (a*Diesel + (1-a)*Raw)||^2 (파형 MSE)
             + w_prop * sum( ((mix_prop_i(a) - fake_prop_i) / scale_i)^2 )  (물성치 정규화 오차)
    """

    def __init__(
        self,
        diesel: FuelSample,
        raw: FuelSample,
        fake: FuelSample,
        wave_weight: float = 1.0,
        prop_weight: float = 1.0,
        prop_scales: Optional[FuelProperties] = None,
    ):
        if not (diesel.intensity.shape == raw.intensity.shape == fake.intensity.shape):
            raise ValueError("경유/원료/가짜석유 파형은 동일한 기준시간축으로 리샘플링되어 있어야 합니다.")

        self.diesel = diesel
        self.raw = raw
        self.fake = fake
        self.wave_weight = wave_weight
        self.prop_weight = prop_weight

        # 물성치별 정규화 스케일 (0 나눗셈 방지겸 서로 다른 단위 균형을 맞추기 위함)
        self.prop_scales = prop_scales or FuelProperties(
            marker_conc=max(abs(fake.properties.marker_conc), 1.0),
            density=max(abs(fake.properties.density), 0.01),
            viscosity=max(abs(fake.properties.viscosity), 0.1),
        )

    # -- 목적함수 -----------------------------------------------------
    def _objective(self, a_arr: np.ndarray) -> float:
        a = float(a_arr[0])

        # 파형 오차 (정규화된 MSE)
        est_wave = mix_waveform(a, self.diesel.intensity, self.raw.intensity)
        wave_err = np.mean((est_wave - self.fake.intensity) ** 2)

        # 물성치 오차 (정규화된 제곱오차 합)
        est_props = mix_properties(a, self.diesel.properties, self.raw.properties)
        fake_props = self.fake.properties
        prop_err = (
            ((est_props.marker_conc - fake_props.marker_conc) / self.prop_scales.marker_conc) ** 2
            + ((est_props.density - fake_props.density) / self.prop_scales.density) ** 2
            + ((est_props.viscosity - fake_props.viscosity) / self.prop_scales.viscosity) ** 2
        )

        return self.wave_weight * wave_err + self.prop_weight * prop_err

    # -- 최적화 실행 ----------------------------------------------------
    def estimate(self, initial_guess: float = 0.7) -> dict:
        """
        SLSQP로 a(경유 부피비율, 0<=a<=1)의 최적값을 탐색한다.

        반환값 (dict):
            a_optimal        : 추정된 경유 비율
            raw_ratio        : 1 - a_optimal (원료 비율)
            success          : 최적화 수렴 여부
            message          : 최적화 결과 메시지
            final_cost       : 최종 목적함수 값
            estimated_waveform: 최적 a에서의 예상 GC 파형
            estimated_properties: 최적 a에서의 예상 물성치
        """
        x0 = np.array([np.clip(initial_guess, 0.0, 1.0)])
        bounds = [(0.0, 1.0)]

        result = minimize(
            self._objective,
            x0,
            method="SLSQP",
            bounds=bounds,
            options={"maxiter": 200, "ftol": 1e-10},
        )

        a_opt = float(np.clip(result.x[0], 0.0, 1.0))
        est_wave, est_props = FuelBlendingSimulator(self.diesel, self.raw).simulate(a_opt)

        return {
            "a_optimal": a_opt,
            "raw_ratio": 1.0 - a_opt,
            "success": bool(result.success),
            "message": str(result.message),
            "final_cost": float(result.fun),
            "estimated_waveform": est_wave,
            "estimated_properties": est_props,
        }


# ---------------------------------------------------------------------------
# (구) Case 2 사전단계: 경유+가짜석유 파형만으로 혼합비율 a를 "맹목적으로" 추정하는
# 시도(estimate_a_from_all_properties)가 이 자리에 있었으나, 검증 결과 원리적으로
# 성립하지 않는 접근이라 제거했다.
#
# 이유: diesel과 fake만 주어지고 원료(raw)가 완전히 미지수인 상태에서는,
#   fake(t) = a*diesel(t) + (1-a)*raw(t)
# 라는 식에서 raw(t)가 "0 이상"이라는 것 외에는 아무 제약이 없으므로, 임의의 a에
# 대해 raw(t) = (fake(t) - a*diesel(t)) / (1-a) 를 그냥 "정답"으로 두면 항상 완벽히
# 재현된다 (파형 재구성오차가 a에 대해 거의 상수 0). 유일하게 남는 제약은
# "raw(t) >= 0"인데 이것만으로는 a 하나의 값을 찍어낼 수 없고(무수히 많은 a가
# 이 조건을 만족), 물성치(식별제/밀도/동점도) 3개 방정식도 미지수 4개(a + 원료
# 물성치 3개)에 비해 방정식이 하나 부족해 근본적으로 미결정계(underdetermined)다.
# 실제로 합성 노이즈 데이터로 검증했을 때 이 옛 함수는 항상 a=0 근처로 발산했다.
#
# 결론: "원료를 전혀 모르는 상태"에서 파형/물성치만으로 a를 자동 추정하는 것은
# 신뢰할 수 없다. 대신 아래 match_fake_against_candidates()처럼, 원료 후보 DB에
# 등록된 실제 후보들을 하나씩 "원료"로 대입해 Case 1과 동일한(이미 검증된)
# MixRatioEstimator로 최적 비율을 구하고, 가장 잘 맞는 후보를 채택하는 방식으로
# 대체했다. 이는 수학적으로 잘 정의된 문제(알려진 3개 신호로 1개 미지수를 맞추는
# 문제)이며, 포렌식 분석 워크플로("등록된 원료들 중 어느 것이 가장 잘 설명하는가")
# 와도 자연스럽게 맞아떨어진다.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Case 2: 원료를 모르는 경우 - 파형 역추정(Deconvolution) + DB 후보 매칭
# ---------------------------------------------------------------------------
def deconvolve_raw_waveform(
    fake_intensity: np.ndarray,
    diesel_intensity: np.ndarray,
    a: float,
) -> np.ndarray:
    """
    미지 원료의 GC 파형을 역추정한다.

        R_est(t) = max(0, (Fake(t) - a*Diesel(t)) / (1 - a))

    Parameters
    ----------
    fake_intensity : 가짜석유 GC 강도 배열
    diesel_intensity : 경유 GC 강도 배열 (fake와 동일 기준시간축)
    a : 가정한 경유 부피비율 (0 <= a < 1). a=1이면 정의 불가하므로 상한을 둔다.
    """
    if fake_intensity.shape != diesel_intensity.shape:
        raise ValueError("가짜석유와 경유 파형의 길이가 일치해야 합니다.")

    a = float(np.clip(a, 0.0, 0.999999))
    denom = 1.0 - a
    raw_est = (fake_intensity - a * diesel_intensity) / denom
    raw_est = np.clip(raw_est, 0.0, None)  # max(0, ...)
    return raw_est


def estimate_unknown_raw_properties(
    a: float,
    fake_props: FuelProperties,
    diesel_props: FuelProperties,
) -> FuelProperties:
    """
    물성 혼합 공식의 역산을 통해 미지 원료의 물성치를 추정한다.

        C_mix = a*C_d + (1-a)*C_r        -> C_r = (C_mix - a*C_d) / (1-a)
        rho_mix = a*rho_d + (1-a)*rho_r  -> rho_r = (rho_mix - a*rho_d) / (1-a)
        ln(nu_mix) = a*ln(nu_d)+(1-a)*ln(nu_r) -> ln(nu_r) = (ln(nu_mix) - a*ln(nu_d)) / (1-a)
    """
    a = float(np.clip(a, 0.0, 0.999999))
    denom = 1.0 - a
    eps = 1e-9

    marker_r = (fake_props.marker_conc - a * diesel_props.marker_conc) / denom
    density_r = (fake_props.density - a * diesel_props.density) / denom

    nu_d = max(diesel_props.viscosity, eps)
    nu_mix = max(fake_props.viscosity, eps)
    ln_nu_r = (np.log(nu_mix) - a * np.log(nu_d)) / denom
    viscosity_r = float(np.exp(ln_nu_r))

    return FuelProperties(
        marker_conc=max(marker_r, 0.0),
        density=max(density_r, 0.0),
        viscosity=max(viscosity_r, 0.0),
    )


def _cosine_sim(vec_a: np.ndarray, vec_b: np.ndarray) -> float:
    """1차원 벡터 두 개의 코사인 유사도를 계산 (sklearn 활용, 2D reshape)."""
    va = vec_a.reshape(1, -1)
    vb = vec_b.reshape(1, -1)
    if np.all(va == 0) or np.all(vb == 0):
        return 0.0
    sim = cosine_similarity(va, vb)[0, 0]
    return float(sim)


def search_similar_raw_materials(
    estimated_raw_waveform: np.ndarray,
    estimated_raw_properties: FuelProperties,
    candidate_db: Sequence[RawMaterialCandidate],
    top_n: int = 5,
    wave_weight: float = 0.9,
    prop_weight: float = 0.1,
) -> List[MatchResult]:
    """
    역추정된 미지 원료 파형/물성치를 원료 DB(candidate_db)와 비교하여
    코사인 유사도 기반 Top-N 후보를 반환한다.

    GC 조성분포 파형이 주력 판별 근거이므로 wave_weight를 크게(기본 0.9) 두고,
    밀도/동점도/식별제 등 물성치는 보조 지표(prop_weight, 기본 0.1)로만 사용한다.
    물성치가 입력되지 않은(0에 가까운) 후보가 많으면 물성치 항은 자동으로 무시된다.

    combined_score = wave_weight * cos_sim(waveform) + prop_weight * (1 - normalized_prop_distance)
    (combined_score가 높을수록 더 유사한 후보)
    """
    if not candidate_db:
        return []

    # 후보들에 물성치 정보가 실제로 있는지 확인 (모두 0이면 물성치 비교 생략)
    has_props = any(
        (c.properties.marker_conc > 0 or c.properties.density > 0 or c.properties.viscosity > 0)
        for c in candidate_db
    )
    if not has_props:
        wave_weight, prop_weight = 1.0, 0.0

    # 물성치 정규화를 위한 스케일 (DB 전체 값 범위를 참고)
    marker_scale = max(1.0, max(c.properties.marker_conc for c in candidate_db))
    density_scale = max(0.01, max(c.properties.density for c in candidate_db))
    viscosity_scale = max(0.1, max(c.properties.viscosity for c in candidate_db))

    est_prop_vec = np.array([
        estimated_raw_properties.marker_conc / marker_scale,
        estimated_raw_properties.density / density_scale,
        estimated_raw_properties.viscosity / viscosity_scale,
    ])

    results: List[MatchResult] = []
    for cand in candidate_db:
        wave_sim = _cosine_sim(estimated_raw_waveform, cand.intensity)

        cand_prop_vec = np.array([
            cand.properties.marker_conc / marker_scale,
            cand.properties.density / density_scale,
            cand.properties.viscosity / viscosity_scale,
        ])
        prop_dist = float(np.linalg.norm(est_prop_vec - cand_prop_vec))
        # 거리를 0~1 유사도 스케일로 변환 (거리가 클수록 유사도 감소, 완만한 감쇠)
        prop_sim = 1.0 / (1.0 + prop_dist) if has_props else 0.0

        combined = wave_weight * wave_sim + prop_weight * prop_sim

        results.append(MatchResult(
            candidate_name=cand.name,
            similarity=wave_sim,
            property_distance=prop_dist,
            combined_score=combined,
        ))

    results.sort(key=lambda r: r.combined_score, reverse=True)
    return results[:top_n]


def match_fake_against_candidates(
    diesel: FuelSample,
    fake: FuelSample,
    candidates: Sequence[RawMaterialCandidate],
    initial_guess: float = 0.7,
    top_n: int = 5,
) -> List[CandidateMatchResult]:
    """
    등록된 원료 후보 DB의 각 후보를 실제 "원료"라고 가정하고, Case 1과 동일한
    MixRatioEstimator(SLSQP)로 최적 혼합비율 a를 구한 뒤, 재구성 오차(final_cost)가
    가장 작은 순으로 정렬해 반환한다.

    원료가 완전히 미지수인 상태에서 파형만으로 a를 추정하는 것은 수학적으로
    미결정계라 신뢰할 수 없지만(analyzer.py 상단 주석 참고), 후보 하나하나는
    "경유/후보/가짜석유가 모두 알려진" Case 1과 동일한 잘 정의된 문제이므로,
    후보를 대입해 보는 방식이 훨씬 안정적이고 정확하다.

    Parameters
    ----------
    diesel, fake : FuelSample
        동일 기준시간축으로 리샘플링된 경유/가짜석유 시료.
    candidates : Sequence[RawMaterialCandidate]
        원료 후보 DB (파형은 diesel/fake와 동일한 기준시간축으로 리샘플링되어 있어야 함).
    initial_guess : float
        SLSQP 초기값 (기본 0.7).
    top_n : int
        반환할 상위 후보 수.
    """
    results: List[CandidateMatchResult] = []
    for cand in candidates:
        if cand.intensity.shape != diesel.intensity.shape:
            continue  # 기준시간축이 다른 후보는 건너뜀 (호출측에서 리샘플링 보장 필요)

        cand_sample = FuelSample(cand.name, diesel.time, cand.intensity, cand.properties)
        estimator = MixRatioEstimator(diesel, cand_sample, fake)
        est = estimator.estimate(initial_guess=initial_guess)
        wave_sim = _cosine_sim(est["estimated_waveform"], fake.intensity)

        results.append(CandidateMatchResult(
            candidate_name=cand.name,
            a_optimal=est["a_optimal"],
            raw_ratio=est["raw_ratio"],
            final_cost=est["final_cost"],
            success=est["success"],
            estimated_properties=est["estimated_properties"],
            wave_similarity=wave_sim,
            candidate=cand,
        ))

    results.sort(key=lambda r: r.final_cost)
    return results[:top_n]


# ---------------------------------------------------------------------------
# 모듈 단독 실행 시 스모크 테스트
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    rng = np.random.default_rng(0)
    t = np.linspace(0, 25, 1000)

    def gauss(center, width, amp):
        return amp * np.exp(-0.5 * ((t - center) / width) ** 2)

    diesel_wave = gauss(12, 1.5, 1.0) + gauss(8, 1.0, 0.4)
    raw_wave = gauss(5, 0.8, 1.0) + gauss(18, 1.2, 0.6)

    true_a = 0.65
    fake_wave = true_a * diesel_wave + (1 - true_a) * raw_wave

    diesel_props = FuelProperties(marker_conc=200.0, density=0.835, viscosity=3.2)
    raw_props = FuelProperties(marker_conc=5.0, density=0.870, viscosity=6.5)
    fake_props = mix_properties(true_a, diesel_props, raw_props)

    diesel = FuelSample("Diesel", t, diesel_wave, diesel_props)
    raw = FuelSample("RawA", t, raw_wave, raw_props)
    fake = FuelSample("FakeSample", t, fake_wave, fake_props)

    # Case 1 테스트
    estimator = MixRatioEstimator(diesel, raw, fake)
    result = estimator.estimate(initial_guess=0.5)
    print(f"[Case1] 실제 a={true_a}, 추정 a={result['a_optimal']:.4f}, "
          f"수렴={result['success']}, cost={result['final_cost']:.2e}")
    assert abs(result["a_optimal"] - true_a) < 1e-3, "Case1 혼합비율 역추적 회귀!"

    # 시뮬레이터 테스트
    sim = FuelBlendingSimulator(diesel, raw)
    wave_sim, props_sim = sim.simulate(0.65)
    print(f"[Simulator] a=0.65 -> density={props_sim.density:.4f}, "
          f"viscosity={props_sim.viscosity:.4f}, marker={props_sim.marker_conc:.2f}")

    # Case 2 테스트 (raw를 모른다고 가정하고 역추정)
    raw_est_wave = deconvolve_raw_waveform(fake_wave, diesel_wave, true_a)
    raw_est_props = estimate_unknown_raw_properties(true_a, fake_props, diesel_props)
    wave_recovery_error = np.mean((raw_est_wave - raw_wave) ** 2)
    print(f"[Case2] 파형 역추정 MSE={wave_recovery_error:.6f}, "
          f"추정 밀도={raw_est_props.density:.4f} (실제 {raw_props.density})")

    db = [
        RawMaterialCandidate("RawA", raw_wave, raw_props),
        RawMaterialCandidate("RawB", gauss(10, 1.0, 1.0), FuelProperties(50.0, 0.85, 4.0)),
        RawMaterialCandidate("RawC", gauss(20, 2.0, 0.8), FuelProperties(2.0, 0.90, 8.0)),
    ]
    matches = search_similar_raw_materials(raw_est_wave, raw_est_props, db, top_n=3)
    print("[Case2] Top matches:")
    for m in matches:
        print(f"  - {m.candidate_name}: score={m.combined_score:.4f}, "
              f"wave_sim={m.similarity:.4f}, prop_dist={m.property_distance:.4f}")

    # Case 2 신규 테스트: DB 후보를 실제로 대입해 최적 비율을 맞추는 방식.
    # (과거 estimate_a_from_all_properties가 노이즈 있는 데이터에서 a=0으로
    #  발산하던 회귀를 다시 잡아내기 위한 테스트 — 반드시 노이즈를 포함시킨다.)
    rng2 = np.random.default_rng(7)
    noisy_fake_wave = fake_wave + 0.01 * rng2.standard_normal(len(t))
    noisy_fake_wave = np.clip(noisy_fake_wave, 0.0, None)
    noisy_fake = FuelSample("FakeNoisy", t, noisy_fake_wave, fake_props)
    matches2 = match_fake_against_candidates(diesel, noisy_fake, db, top_n=3)
    print("[Case2] match_fake_against_candidates 결과:")
    for m in matches2:
        print(f"  - {m.candidate_name}: a={m.a_optimal:.4f}, cost={m.final_cost:.3e}, "
              f"wave_sim={m.wave_similarity:.4f}")
    assert matches2[0].candidate_name == "RawA", "가장 잘 맞는 후보를 못 찾음 (회귀!)"
    assert abs(matches2[0].a_optimal - true_a) < 0.05, "노이즈 있는 데이터에서 혼합비율 추정 회귀!"

    print("\n[전체 통과] 모든 스모크 테스트 검증 완료.")
