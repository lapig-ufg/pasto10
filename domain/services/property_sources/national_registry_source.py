"""Where a country plugs in its own property registry.

Pasto10 is a showcase: it cannot know the property boundaries of your country.
Replace this class (or subclass it) with a reader of your national registry,
for example a rural environmental registry, an agricultural parcel system such
as an EU LPIS, a cadastre web service or a database export, and enable it with
``PASTO10_PROPERTY_SOURCES=registry`` (or the ``property_sources`` key of the
manifest, see ``registry.py``).

Contract
--------
Input of ``find_by_point``: a WGS84 coordinate and a search radius in meters.
Output: a list of ``PropertyCandidate`` (``domain/schemas/candidate.py``) with
``origin=GeometryOrigin.REGISTERED_BOUNDARY``, a GeoJSON ``geometry`` in lon/lat
(Polygon or MultiPolygon), ``area_ha``, ``distance_m`` (0 when the point is
inside), the registry number in ``candidate_id`` and the data date in
``source_date``. Raise ``PropertySourceError("unavailable" | "timeout")`` when
the registry cannot answer; return ``[]`` when nothing is found.
``find_by_id`` returns the candidate of a registry number or ``None``.

Example (commented, adapt to your service)::

    class MyRegistrySource(NationalRegistrySource):
        key = "registry"

        def find_by_point(self, latitude, longitude, search_radius_m=2000):
            rows = query_registry(lat=latitude, lon=longitude, radius=search_radius_m)
            return [
                PropertyCandidate(
                    candidate_id=row["registry_number"], name=row["owner_label"],
                    origin=GeometryOrigin.REGISTERED_BOUNDARY, source=self.key,
                    source_date=row["updated_at"], geometry=row["geojson"], area_ha=row["area_ha"],
                    distance_m=row["distance_m"], license="national open data licence",
                )
                for row in rows
            ]
"""

from typing import Optional

from domain.schemas.candidate import PropertyCandidate


class NationalRegistrySource:
    """Placeholder for a national property registry (disabled by default).

    Attributes:
        key (str): ``"registry"``.
    """

    key = "registry"

    def find_by_point(self, latitude: float, longitude: float, search_radius_m: float = 2000) -> list[PropertyCandidate]:
        """Would return the registered properties around a coordinate.

        Args:
            latitude (float): Latitude in decimal degrees.
            longitude (float): Longitude in decimal degrees.
            search_radius_m (float): Search radius in meters.

        Returns:
            list[PropertyCandidate]: See the module docstring for the contract.

        Raises:
            NotImplementedError: Always; implement it for your country.
        """
        raise NotImplementedError("Implement NationalRegistrySource.find_by_point for your country's property registry.")

    def find_by_id(self, identifier: str) -> Optional[PropertyCandidate]:
        """Would return the property of a registry number.

        Args:
            identifier (str): Registry number.

        Returns:
            PropertyCandidate | None: See the module docstring for the contract.

        Raises:
            NotImplementedError: Always; implement it for your country.
        """
        raise NotImplementedError("Implement NationalRegistrySource.find_by_id for your country's property registry.")
