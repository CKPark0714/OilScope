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
# Case 2 사전단계: 식별제+파형 기반 혼합비율(a) 추정 (원료를 모를 때)
# ---------------------------------------------------------------------------
def estimate_a_from_all_properties(
    fake_intensity: np.ndarray,
    diesel_intensity: np.ndarray,
    fake_props: FuelProperties,
    diesel_props: FuelProperties,
    raw_prop_bounds: Optional[dict] = None,
) -> dict:
    """
    원료를 모를 때, **식별제 + 밀도 + 동점도 + 파형**을 모두 활용해
    혼합비율 a(경유 부피비율)를 추정한다.

    접근법: 경유 외에 "원료"라고 볼 수 있는 신호 성분을 가짜석유에서 추정하고,
            SLSQP로 "가짜석유 = a*경유 + (1-a)*원료"를 가장 잘 만족하면서
            역산된 원료 물성치가 타당 범위에 들어가는 a를 찾는다.

        목적함수 J(a) = w_wave * 재구성오차(a) + w_prop * 물성범위위반(a)

        재구성오차(a): fake와 a*diesel의 차이(양수부분)를 원료 proxy로 쓰고,
                       혼합 모델 a*diesel + (1-a)*proxy 가 fake를 얼마나 잘
                       재현하는지를 측정한다.
        물성범위위반(a): 식별제/밀도/동점도 역산값이 [raw_prop_bounds] 범위를
                       벗어나는 정도.

    Parameters
    ----------
    raw_prop_bounds : dict, optional
        미지 원료 물성치의 타당 범위. 기본값:
            {"marker": (0, 500), "density": (0.75, 0.95), "viscosity": (0.5, 20.0)}
    """
    if raw_prop_bounds is None:
        raw_prop_bounds = {
            "marker": (0.0, 500.0),
            "density": (0.75, 0.95),
            "viscosity": (0.5, 20.0),
        }

    # --- 원료 proxy: a의 그리드 전체에서 NNLS로 가장 잘 맞는 원료 성분을 추정 ---
    from scipy.optimize import nnls as _nnls

    fake_max = fake_intensity.max()
    diesel_max = diesel_intensity.max()
    # 정규화된 파형 간 스케일 차이를 흡수하기 위한 스케일 비
    scale_ratio = (fake_max / diesel_max) if diesel_max > 0 else 1.0

    best_a, best_err = 0.7, np.inf
    for a in np.linspace(0.05, 0.95, 91):
        # fake ≈ alpha*diesel + beta*raw_proxy 형태로 맞추기 위해,
        # 우선 fake에서 a*scale_ratio*diesel 기여분을 빼고 나머지를 원료 성분으로 본다.
        residual = fake_intensity - a * scale_ratio * diesel_intensity
        # 물리적으로 음수는 불가 -> 과도한 음수는 패널티
        neg_penalty = float(np.sum(np.clip(-residual, 0.0, None)) ** 2)
        pos_residual = np.clip(residual, 0.0, None)
        # 나머지(양수부분)를 원료 파형 proxy로 사용했을 때 재구성 오차
        est = a * scale_ratio * diesel_intensity + (1.0 - a) * pos_residual / max(pos_residual.max(), 1e-12)
        err = float(np.mean((est - fake_intensity) ** 2)) + 0.1 * neg_penalty
        if err < best_err:
            best_err = err
            best_a = float(a)

    def raw_props_at(a: float) -> FuelProperties:
        return estimate_unknown_raw_properties(a, fake_props, diesel_props)

    def prop_violation(a: float) -> float:
        """역산된 원료 물성치가 타당 범위를 벗어나는 정도 (작을수록 좋음)."""
        rp = raw_props_at(a)
        v = 0.0
        lo, hi = raw_prop_bounds["marker"]
        v += max(0.0, lo - rp.marker_conc) + max(0.0, rp.marker_conc - hi)
        lo, hi = raw_prop_bounds["density"]
        v += (max(0.0, lo - rp.density) + max(0.0, rp.density - hi)) * 100.0
        lo, hi = raw_prop_bounds["viscosity"]
        v += (max(0.0, lo - rp.viscosity) + max(0.0, rp.viscosity - hi)) * 0.1
        return v

    # 파형 기반 best_a 주변을 물성 제약과 함께 SLSQP로 미세 조정
    def objective(a_arr: np.ndarray) -> float:
        a = float(a_arr[0])
        residual = fake_intensity - a * scale_ratio * diesel_intensity
        neg_penalty = float(np.sum(np.clip(-residual, 0.0, None)) ** 2)
        return 10.0 * neg_penalty + 1.0 * prop_violation(a)

    result = minimize(
        objective,
        x0=np.array([best_a]),
        method="SLSQP",
        bounds=[(0.0, 0.999)],
        options={"maxiter": 300, "ftol": 1e-12},
    )
    best_a = float(np.clip(result.x[0], 0.0, 0.999))
    est_raw = raw_props_at(best_a)

    return {
        "a_optimal": best_a,
        "raw_ratio": 1.0 - best_a,
        "method": "all_properties+waveform(SLSQP)",
        "estimated_raw_properties": est_raw,
        "cost": float(result.fun),
        "success": bool(result.success),
    }


# ---------------------------------------------------------------------------
# Case 2: 원료를 모르는 경우 - 파형 역추정(Deconvolution) + DB 유사도 탐색
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
