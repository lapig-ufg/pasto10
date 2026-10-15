from uuid import uuid4
from typing import List, Optional, Union

from pydantic import BaseModel, Field


class FeatureMetadata(BaseModel):
    """A single arbitrary attribute attached to a feature.

    Any feature type can carry any key/value pair (e.g. ``radius``,
    ``center``, ``name``), so new feature types never require new schemas.
    """

    key: str = Field(
        ...,
        description="Attribute name (e.g. radius, center, name).",
        examples=["radius"]
    )

    value: Union[str, float, int] = Field(
        ...,
        description="Attribute value.",
        examples=[200]
    )


class Feature(BaseModel):
    """General schema representing any geographic feature in the system.

    Every registrable feature — buffer areas today, any future type later —
    is represented by this single model. Type-specific data lives in the
    free-form ``metadata`` list; ``feature_type`` holds the neutral key
    identifying the type (never localized; display names live in
    ``agents.yml`` under ``feature_type_names``).

    The ``feature_id`` is a stable, generated identifier used by the tools;
    the user-chosen display name is stored separately in metadata
    (``name``) so renaming never changes the id.
    """

    feature_id: Optional[str] = Field(
        default=None,
        description="Stable unique identifier of the feature."
    )

    coords: List[List[List[List[float]]]] = Field(
        ...,
        description="Geographic coordinates of the feature (GeoJSON MultiPolygon nesting, [lon, lat] pairs)."
    )

    metadata: List[FeatureMetadata] = Field(
        default_factory=list,
        description="Type-specific attributes of the feature (key/value pairs)."
    )

    total_area: float = Field(
        ...,
        description="Total area of the feature in hectares.",
        examples=[12.57]
    )

    feature_type: str = Field(
        ...,
        description="Neutral key identifying the feature type (e.g. buffer_area)."
    )

    @property
    def id(self) -> Optional[str]:
        """The stable feature identifier (alias of ``feature_id``)."""
        return self.feature_id

    @property
    def name(self) -> Optional[str]:
        """The user-chosen display name (stored as metadata), if any."""
        return self.get_metadata("name")

    def get_metadata(self, key: str, default=None):
        """Returns the metadata value stored under a key.

        Args:
            key (str): Metadata key to look up.
            default: Value returned when the key is absent.

        Returns:
            The stored value (str, float or int), or ``default``.
        """
        for entry in self.metadata:
            if entry.key == key:
                return entry.value
        return default

    def get_coords(self) -> List[List[List[List[float]]]]:
        """Returns the GeoJSON MultiPolygon coordinates of the feature.

        Returns:
            List[List[List[List[float]]]]: Polygons -> rings -> ``[lon, lat]``
            pairs; the first ring of the first polygon is the outer boundary.
        """
        return self.coords

    def describe(self) -> str:
        """Builds a one-line, LLM-readable description of the feature.

        The text lists the id, the display name (when set), the neutral type,
        the area in hectares and every metadata entry except ``name``. It is
        used in tool results and in the agent instructions.

        Returns:
            str: The feature description.
        """
        details = ", ".join(
            f"{entry.key}: {entry.value}"
            for entry in self.metadata
            if entry.key != "name"
        )
        metadata = f", {details}" if details else ""
        name = f", Name: {self.name}" if self.name else ""
        return (
            f"Feature ID: {self.id}{name}, "
            f"Type: {self.feature_type}, "
            f"Area: {self.total_area} ha{metadata}."
        )

    def __str__(self):
        return self.describe()

    @staticmethod
    def generate_id() -> str:
        """Generates a short unique id for a new feature.

        Returns:
            str: The first 12 hexadecimal characters of a random UUID4.
        """
        return uuid4().hex[:12]


class RegisteredFeatures(BaseModel):
    """All features registered by the user in the current session.

    The collection is stored in ``session_state["all_properties"]`` as its
    ``model_dump()`` (a ``{"features": [...]}`` dict); read and write it only
    through ``get_registered_features`` / ``set_registered_features`` in
    ``domain/utils/feature_utils.py``, which handle the (de)serialization.
    """

    features: List[Feature] = Field(
        default_factory=list,
        description="Features registered in the system."
    )

    def build_prompt(self) -> str:
        """Builds a prompt block describing every registered feature.

        Features are clustered by ``feature_type`` inside
        ``<feature_type>...</feature_type>`` tags, one ``describe()`` line
        per feature.

        Returns:
            str: The prompt block, or an empty string when nothing is registered.
        """
        if not self.features:
            return ""

        clusters: dict[str, List[str]] = {}
        for feature in self.features:
            clusters.setdefault(feature.feature_type, []).append(feature.describe())

        parts: List[str] = []
        for feature_type, descriptions in clusters.items():
            body = "\n".join(f"  - {description}" for description in descriptions)
            parts.append(f"<{feature_type}>\n{body}\n</{feature_type}>")

        return "\n".join(parts)
