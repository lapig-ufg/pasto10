"""Tests of the season onset heuristic (domain/services/geospatial/season_forecast.py). Pure, no I/O."""

from datetime import date

import pytest

from domain.services.geospatial import season_forecast as sf

TODAY = date(2026, 10, 22)


def _members(daily, count=5, days=100):
    """Members whose daily rain is ``daily(day_index)`` mm."""
    return [[daily(index) for index in range(days)] for _ in range(count)]


def test_documented_constants_live_in_one_place():
    assert (sf.WINDOW_DAYS, sf.WET_MM, sf.DRY_MM, sf.PERSISTENCE_DAYS) == (30, 60.0, 30.0, 7)


def test_horizon_stops_at_the_first_day_with_a_missing_member():
    members = [[1.0, 1.0, 1.0, None], [1.0, 1.0, None, None]]

    assert sf.horizon_days(members) == 2
    assert sf.horizon_days([]) == 0


def test_rolling_totals_combine_past_and_forecast_days():
    past = [1.0] * 30
    members = [[10.0, 10.0, 10.0]]

    totals = sf.rolling_median_totals(past, members)

    assert totals == [39.0, 48.0, 57.0]


def test_rain_onset_from_a_dry_start():
    past = [0.0] * 30
    members = _members(lambda i: 0.0 if i < 10 else 6.0)

    result = sf.forecast_season("rain", TODAY, past, members)

    assert result.status == "onset" and result.current_state == "dry"
    assert result.days_ahead == 20 and result.onset_date == date(2026, 11, 11)
    assert sf.forecast_season("dry", TODAY, past, members).status == "underway"


def test_wet_start_is_underway_for_rain_and_gives_a_dry_onset():
    past = [3.0] * 30
    members = _members(lambda i: 0.0)

    rain = sf.forecast_season("rain", TODAY, past, members)
    dry = sf.forecast_season("dry", TODAY, past, members)

    assert rain.status == "underway" and rain.current_state == "wet"
    assert dry.status == "onset" and dry.days_ahead == 20


def test_not_within_horizon_when_nothing_changes():
    past = [0.0] * 30
    members = _members(lambda i: 0.2)

    assert sf.forecast_season("rain", TODAY, past, members).status == "not_within_horizon"
    assert sf.forecast_season("dry", TODAY, past, members).status == "underway"


def test_wet_start_stays_not_within_horizon_for_dry_when_rain_continues():
    result = sf.forecast_season("dry", TODAY, [3.0] * 30, _members(lambda i: 3.0))

    assert result.status == "not_within_horizon"


def test_hysteresis_each_season_starts_only_from_the_opposite_state():
    past = [3.0] * 30
    members = _members(lambda i: 0.0 if i < 60 else 8.0, days=120)

    events = sf.season_events(sf.WET, sf.rolling_median_totals(past, members))

    assert [kind for kind, _ in events] == ["dry", "rain"]
    assert sf.forecast_season("rain", TODAY, past, members).status == "onset"


def test_a_short_spike_does_not_start_the_rainy_season():
    past = [0.0] * 30
    members = _members(lambda i: 20.0 if 10 <= i < 13 else 0.0)

    assert sf.forecast_season("rain", TODAY, past, members).status == "not_within_horizon"


def test_median_over_members_ignores_a_minority_of_wet_members():
    past = [0.0] * 30
    wet = [8.0] * 100
    dry = [0.0] * 100

    result = sf.forecast_season("rain", TODAY, past, [wet, dry, dry, dry, dry])

    assert result.status == "not_within_horizon"


def test_horizon_is_limited_by_null_tails_and_reported():
    members = [[0.0] * 40 + [None] * 10]

    assert sf.forecast_season("rain", TODAY, [0.0] * 30, members).horizon_days == 40


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        sf.forecast_season("snow", TODAY, [0.0] * 30, _members(lambda i: 0.0))
