"""LearnedWeights: the user's taste tables finally steer sampling (2026-09-04).

For seven months music_{track,artist,genre}_weights were write-only for
selection — the boost/ban buttons adjusted numbers only the UI displayed, and
a banned artist (factor 0.0) sampled exactly like a loved one. These tests pin
the read side: deterministic key joins across both track-key generations, Ban
meaning zero, and preferred_genres replacing the hardcoded boost lists.

Hermetic: pure dataclass, no DB (load_learned_weights is a trivial three-SELECT
pass exercised in production; the composition logic is what needs pinning).
"""
from __future__ import annotations

from app.assistant.dj_manager.learned_weights import LearnedWeights


def _lw():
    return LearnedWeights(
        track={
            "spotify:track123": 1.5,                     # current key generation
            "handshake drugs|||wilco": 0.9,              # legacy 2026-02 generation
        },
        artist={"pink floyd": 2.0, "broadcast": 0.0},
        genre={"alt-rock": 2.5, "afrobeat": 2.0, "chill": 0.5},
    )


class TestFactorComposition:
    def test_unknown_song_is_neutral(self):
        assert _lw().factor_for(track_id="zzz", title="X", artist="Y", genre="z") == 1.0

    def test_current_track_key_generation(self):
        f = _lw().factor_for(track_id="TRACK123", title="whatever", artist="whoever", genre="")
        assert f == 1.5, "spotify:<id> keys must join case-insensitively"

    def test_legacy_track_key_generation_still_joins(self):
        f = _lw().factor_for(track_id="not_in_table", title="Handshake Drugs",
                             artist="Wilco", genre="")
        assert f == 0.9, "2026-02 '<title>|||<artist>' rows must keep working"

    def test_factors_multiply_across_scopes(self):
        f = _lw().factor_for(track_id="", title="Time", artist="Pink Floyd", genre="alt-rock")
        assert f == 2.0 * 2.5

    def test_ban_zeroes_the_product(self):
        """The Ban button's actual promise: a 0.0 anywhere means never sampled."""
        f = _lw().factor_for(track_id="", title="Colour Me In", artist="Broadcast",
                             genre="alt-rock")
        assert f == 0.0, "a banned artist must zero out even a boosted genre"

    def test_downweight_scales_below_one(self):
        f = _lw().factor_for(track_id="", title="t", artist="a", genre="Chill")
        assert f == 0.5


class TestPreferredGenres:
    def test_only_boosted_genres_qualify(self):
        assert _lw().preferred_genres() == {"alt-rock", "afrobeat"}

    def test_empty_tables_mean_no_boosts(self):
        """Fresh install: no learned genres -> empty boost set -> sampling stays
        purely distance+prob_factor driven, and no hardcoded list sneaks back."""
        assert LearnedWeights().preferred_genres() == set()
