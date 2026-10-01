"""
regions.py

Looks up which administrative area (level 1: county/province/region,
level 2: sub-county/district) a coordinate falls in, using the bundled
boundary files in regions/ (see regions/README.md for sources).
"""

import json
import os
from functools import lru_cache

REGIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "regions")
LEVELS = (1, 2)


def boundary_path(country, level):
    return os.path.join(REGIONS_DIR, f"{country}-{level}.geojson")


@lru_cache(maxsize=None)
def _areas(country, level):
    """[(name, bbox, polygons)] for one country/level, or [] if no file exists."""
    path = boundary_path(country, level)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    areas = []
    for feature in data["features"]:
        polygons = feature["geometry"]["coordinates"]
        xs = [x for poly in polygons for x, _ in poly[0]]
        ys = [y for poly in polygons for _, y in poly[0]]
        areas.append((feature["properties"]["name"], (min(xs), min(ys), max(xs), max(ys)), polygons))
    return areas


def _in_ring(lon, lat, ring):
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _in_polygon(lon, lat, polygon):
    outer, *holes = polygon
    return _in_ring(lon, lat, outer) and not any(_in_ring(lon, lat, hole) for hole in holes)


def area_of(country, level, lat, lon):
    for name, (min_x, min_y, max_x, max_y), polygons in _areas(country, level):
        if min_x <= lon <= max_x and min_y <= lat <= max_y and any(_in_polygon(lon, lat, p) for p in polygons):
            return name
    return None


def tag_regions(stops, country):
    """Adds region (level 1) and district (level 2) names to each stop dict in place."""
    for stop in stops:
        stop["region"] = area_of(country, 1, stop["lat"], stop["lon"])
        stop["district"] = area_of(country, 2, stop["lat"], stop["lon"])
    return stops
