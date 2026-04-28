"""
Music feature scaling utilities.

We want LLM-friendly "0-100 sliders" while the dataset uses native units:
- Most audio features are 0.0 to 1.0 (Spotify-style)
- loudness is in dB (roughly -60 to +5 depending on mastering)
- tempo is BPM

This module provides deterministic conversions both ways:
- llm_to_db_features(): sliders (0-100) -> dataset/native units
- db_to_llm_features(): dataset/native units -> sliders (0-100)

Notes:
- For loudness and tempo we map to robust percentiles (p5..p95) to avoid
  extreme outliers dominating the scale.
- Default percentiles are taken from `data/music_data/curated_music_data.csv`
  computed on 2026-01-17:
    loudness p5=-19.464, p95=-3.433
    tempo    p5= 76.783, p95=175.797
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _to_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except Exception:
        return None


def _slider01(slider_0_100: float) -> float:
    """Map 0-100 slider to 0.0-1.0."""
    return _clamp(slider_0_100, 0.0, 100.0) / 100.0


def _from01_to_slider(x01: float) -> float:
    """Map 0.0-1.0 to 0-100 slider."""
    return _clamp(x01, 0.0, 1.0) * 100.0


def _lerp(a: float, b: float, t01: float) -> float:
    t = _clamp(t01, 0.0, 1.0)
    return a + (b - a) * t


def _inv_lerp(a: float, b: float, v: float) -> float:
    if b == a:
        return 0.0
    return _clamp((v - a) / (b - a), 0.0, 1.0)


@dataclass(frozen=True)
class FeatureScale:
    """
    Defines how to map a slider (0-100) to a native numeric range.
    """

    lo: float
    hi: float

    def slider_to_native(self, slider_0_100: float) -> float:
        return _lerp(self.lo, self.hi, _slider01(slider_0_100))

    def native_to_slider(self, native_value: float) -> float:
        return _from01_to_slider(_inv_lerp(self.lo, self.hi, native_value))


# Robust scales for non-0..1 features
DEFAULT_LOUDNESS_DB_SCALE = FeatureScale(lo=-19.464, hi=-3.433)  # p5..p95
DEFAULT_TEMPO_BPM_SCALE = FeatureScale(lo=76.783, hi=175.797)  # p5..p95


def llm_to_db_features(
    llm_features: Dict[str, Any],
    *,
    loudness_db_scale: FeatureScale = DEFAULT_LOUDNESS_DB_SCALE,
    tempo_bpm_scale: FeatureScale = DEFAULT_TEMPO_BPM_SCALE,
) -> Dict[str, float]:
    """
    Convert LLM 0-100 sliders -> dataset/native units.

    Expected keys (all optional):
    - energy, valence, speechiness, acousticness, instrumentalness, liveness: 0-100 -> 0.0-1.0
    - loudness: 0-100 -> dB via loudness_db_scale
    - tempo: 0-100 -> BPM via tempo_bpm_scale
    """
    out: Dict[str, float] = {}

    # 0..1 features
    for k in (
        "energy",
        "valence",
        "speechiness",
        "acousticness",
        "instrumentalness",
        "liveness",
    ):
        v = _to_float(llm_features.get(k))
        if v is None:
            continue
        out[k] = _slider01(v)

    # loudness (dB)
    v = _to_float(llm_features.get("loudness"))
    if v is not None:
        out["loudness"] = loudness_db_scale.slider_to_native(v)

    # tempo (BPM)
    v = _to_float(llm_features.get("tempo"))
    if v is not None:
        out["tempo"] = tempo_bpm_scale.slider_to_native(v)

    return out


def db_to_llm_features(
    db_features: Dict[str, Any],
    *,
    loudness_db_scale: FeatureScale = DEFAULT_LOUDNESS_DB_SCALE,
    tempo_bpm_scale: FeatureScale = DEFAULT_TEMPO_BPM_SCALE,
) -> Dict[str, float]:
    """
    Convert dataset/native units -> LLM 0-100 sliders.

    Expected keys (all optional):
    - energy, valence, speechiness, acousticness, instrumentalness, liveness: 0.0-1.0 -> 0-100
    - loudness: dB -> 0-100 via loudness_db_scale
    - tempo: BPM -> 0-100 via tempo_bpm_scale
    """
    out: Dict[str, float] = {}

    for k in (
        "energy",
        "valence",
        "speechiness",
        "acousticness",
        "instrumentalness",
        "liveness",
    ):
        v = _to_float(db_features.get(k))
        if v is None:
            continue
        out[k] = round(_from01_to_slider(v), 1)

    v = _to_float(db_features.get("loudness"))
    if v is not None:
        out["loudness"] = round(loudness_db_scale.native_to_slider(v), 1)

    v = _to_float(db_features.get("tempo"))
    if v is not None:
        out["tempo"] = round(tempo_bpm_scale.native_to_slider(v), 1)

    return out


def valence_slider_to_signed(valence_slider_0_100: float) -> float:
    """
    Convenience: map LLM valence slider (0..100) to signed valence (-1..+1).
    0 => -1.0, 50 => 0.0, 100 => +1.0
    """
    return (_slider01(valence_slider_0_100) * 2.0) - 1.0


def signed_valence_to_slider(valence_signed: float) -> float:
    """
    Convenience: map signed valence (-1..+1) to LLM slider (0..100).
    """
    return _from01_to_slider((_clamp(valence_signed, -1.0, 1.0) + 1.0) / 2.0)

