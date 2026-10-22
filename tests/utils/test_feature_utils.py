"""Tests of the area lookup (domain/utils/feature_utils.py) and the shared tool helpers."""

import pytest
from semente import ToolResult

from domain.schemas.feature import FeatureMetadata, RegisteredFeatures
from domain.services.geospatial.geometry import build_buffered_area
from domain.utils.feature_utils import find_registered_feature, set_registered_features
from domain.utils.tool_utils import unexpected_error


def _state(*names):
    features = []
    for index, name in enumerate(names):
        feature = build_buffered_area(-15.5 + index, -47.5, 300)
        if name:
            feature.metadata.append(FeatureMetadata(key="name", value=name))
        features.append(feature)
    state = {}
    set_registered_features(state, RegisteredFeatures(features=features))
    return state, features


def test_nothing_registered_returns_none():
    assert find_registered_feature({}, None) is None
    assert find_registered_feature({}, "North") is None


def test_without_name_returns_latest():
    state, features = _state("North Paddock", "South Paddock")

    assert find_registered_feature(state).feature_id == features[1].feature_id


def test_exact_match_is_case_insensitive():
    state, features = _state("North Paddock", "South Paddock")

    assert find_registered_feature(state, "  north paddock ").feature_id == features[0].feature_id


def test_unique_partial_match():
    state, features = _state("North Paddock", "South Paddock")

    assert find_registered_feature(state, "south").feature_id == features[1].feature_id


def test_ambiguous_or_unknown_name_returns_none():
    state, _ = _state("North Paddock", "South Paddock")

    assert find_registered_feature(state, "paddock") is None
    assert find_registered_feature(state, "East") is None


def test_lookup_does_not_modify_state():
    state, _ = _state("North Paddock")
    before = repr(state)

    find_registered_feature(state, "North")

    assert repr(state) == before


def test_unexpected_error_returns_generic_text_without_details():
    try:
        raise RuntimeError("secret detail")
    except RuntimeError:
        result = unexpected_error("pasture_tools", "get_pasture_area")

    assert isinstance(result, ToolResult)
    assert "unexpected error" in result.content and "secret detail" not in result.content


def test_area_label_and_lookup_area_are_shared_by_the_tools():
    from semente.backends.toolkit import StateContext

    from domain.utils.tool_utils import area_label, lookup_area

    state, features = _state("North Paddock", None)
    context = StateContext(session_state=state)

    assert area_label(features[0]) == "North Paddock" and area_label(features[1]) != ""
    found, failure = lookup_area(context, "north", "pasture_tools", "get_pasture_area")
    assert failure is None and found.feature_id == features[0].feature_id
    _, unknown = lookup_area(context, "lake", "pasture_tools", "get_pasture_area")
    _, empty = lookup_area(StateContext(session_state={}), None, "biomass_tools", "get_biomass_total")
    assert "lake" in unknown.content and "no registered area" in empty.content
