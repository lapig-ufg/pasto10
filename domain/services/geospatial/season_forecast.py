"""Rain and dry season onset from a precipitation ensemble (pure algorithm, no I/O).

A deliberately simple global heuristic, meant as a demonstration: it works for
regions with a marked wet/dry cycle, and seasonal forecasts have low skill.

For each ensemble member the 30-day rolling precipitation total is computed
over the last observed days followed by the member's forecast days; the median
over the members gives one series. The initial state is wet when the last
``WINDOW_DAYS`` days had at least ``WET_MM`` of rain, otherwise dry. Seasons
alternate (hysteresis): from a dry state the rainy season starts on the first
day the total exceeds ``WET_MM`` and stays above it for ``PERSISTENCE_DAYS``
days; from a wet state the dry season starts on the first day the total falls
below ``DRY_MM``.
"""

import statistics
from datetime import date, timedelta
from typing import Optional, Sequence

from domain.schemas.weather import SeasonForecast

WINDOW_DAYS = 30
WET_MM = 60.0
DRY_MM = 30.0
PERSISTENCE_DAYS = 7
WET = "wet"
DRY = "dry"


def horizon_days(members: Sequence[Sequence[Optional[float]]]) -> int:
    """Counts the leading forecast days where every member has a value.

    Args:
        members (Sequence[Sequence[float | None]]): Daily precipitation of each member, today first.

    Returns:
        int: Number of valid leading days (0 when there are no members).
    """
    if not members:
        return 0
    horizon = 0
    for values in zip(*members):
        if any(value is None for value in values):
            break
        horizon += 1
    return horizon


def rolling_median_totals(past_mm: Sequence[float], members: Sequence[Sequence[Optional[float]]]) -> list[float]:
    """Median over the members of the ``WINDOW_DAYS`` rolling precipitation total, per forecast day.

    Args:
        past_mm (Sequence[float]): The last ``WINDOW_DAYS`` observed daily values, ending yesterday.
        members (Sequence[Sequence[float | None]]): Daily precipitation of each member, today first.

    Returns:
        list[float]: One total per valid forecast day (today first).
    """
    valid = horizon_days(members)
    history = list(past_mm)[-WINDOW_DAYS:]
    totals = []
    for index in range(valid):
        window_sums = []
        for member in members:
            series = history + [float(value) for value in member[: index + 1]]
            window_sums.append(sum(series[-WINDOW_DAYS:]))
        totals.append(statistics.median(window_sums))
    return totals


def season_events(initial_state: str, totals: Sequence[float]) -> list[tuple[str, int]]:
    """Finds the alternating season onsets in a series of rolling totals.

    Args:
        initial_state (str): ``WET`` or ``DRY`` at the start.
        totals (Sequence[float]): Median rolling totals, today first.

    Returns:
        list[tuple[str, int]]: ``("rain", day_index)`` and ``("dry", day_index)`` events in order.
    """
    events: list[tuple[str, int]] = []
    state = initial_state
    index = 0
    while index < len(totals):
        if state == DRY:
            window = totals[index: index + PERSISTENCE_DAYS]
            if len(window) == PERSISTENCE_DAYS and all(total > WET_MM for total in window):
                events.append(("rain", index))
                state = WET
        elif totals[index] < DRY_MM:
            events.append(("dry", index))
            state = DRY
        index += 1
    return events


def forecast_season(kind: str, today: date, past_mm: Sequence[float], members: Sequence[Sequence[Optional[float]]]) -> SeasonForecast:
    """Predicts when the rainy (``kind="rain"``) or dry (``"dry"``) season starts.

    Args:
        kind (str): ``"rain"`` or ``"dry"``.
        today (date): Local date of the first forecast day.
        past_mm (Sequence[float]): The last observed days ending yesterday (``WINDOW_DAYS`` expected).
        members (Sequence[Sequence[float | None]]): Daily precipitation of each ensemble member, today first.

    Returns:
        SeasonForecast: ``onset`` with the date when the season starts within
        the horizon; ``underway`` when it is already running now and does not
        restart within the horizon; ``not_within_horizon`` otherwise.

    Raises:
        ValueError: If ``kind`` is not ``"rain"`` or ``"dry"``.
    """
    if kind not in ("rain", "dry"):
        raise ValueError(f"Unknown season kind: {kind!r}")
    recent = float(sum(list(past_mm)[-WINDOW_DAYS:]))
    initial = WET if recent >= WET_MM else DRY
    totals = rolling_median_totals(past_mm, members)
    events = [index for event, index in season_events(initial, totals) if event == kind]
    common = dict(kind=kind, horizon_days=len(totals), current_state=initial, recent_mm=round(recent, 1))
    if events:
        return SeasonForecast(status="onset", onset_date=today + timedelta(days=events[0]), days_ahead=events[0], **common)
    running = (kind == "rain" and initial == WET) or (kind == "dry" and initial == DRY)
    return SeasonForecast(status="underway" if running else "not_within_horizon", **common)
