"""
locations.py — Location configuration and data model

Blinkit uses lat/lng headers to resolve the nearest dark store.
NOTE: Session-based location context may be sticky. If results look wrong
for a given location (showing inventory from a different city), you may
need to refresh the session after changing location, or use separate
Playwright contexts per location.

Start simple (swap headers) → escalate only if results are incorrect.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Location:
    """Represents a delivery location for Blinkit scanning."""
    name: str
    lat: float
    lng: float
    description: Optional[str] = None

    def to_headers(self) -> dict[str, str]:
        """Return Blinkit location headers for this location."""
        return {
            "lat": str(self.lat),
            "lon": str(self.lng),
        }

    def __str__(self) -> str:
        return f"{self.name} ({self.lat:.4f}, {self.lng:.4f})"


def load_locations(config: dict) -> list[Location]:
    """Load and validate locations from parsed config.yaml data."""
    raw = config.get("locations", [])
    if not raw:
        raise ValueError(
            "No locations defined in config.yaml. "
            "Add at least one location with name, lat, and lng."
        )

    locations = []
    for i, loc in enumerate(raw):
        name = loc.get("name")
        lat = loc.get("lat")
        lng = loc.get("lng")

        if not name:
            raise ValueError(f"Location #{i+1} is missing a 'name' field.")
        if lat is None or lng is None:
            raise ValueError(f"Location '{name}' is missing lat or lng.")

        try:
            lat = float(lat)
            lng = float(lng)
        except (TypeError, ValueError):
            raise ValueError(f"Location '{name}' has invalid lat/lng values.")

        if not (-90 <= lat <= 90):
            raise ValueError(f"Location '{name}' has invalid latitude: {lat}")
        if not (-180 <= lng <= 180):
            raise ValueError(f"Location '{name}' has invalid longitude: {lng}")

        locations.append(Location(name=name, lat=lat, lng=lng))

    if len(locations) > 5:
        import warnings
        warnings.warn(
            f"You have {len(locations)} locations configured. "
            "More than 5 increases detection risk significantly. "
            "Consider reducing to 3–5 for safer operation.",
            UserWarning,
            stacklevel=2,
        )

    return locations
