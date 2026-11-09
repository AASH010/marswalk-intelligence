from typing import List, Literal, Tuple
from pydantic import BaseModel, Field


class RouteRequest(BaseModel):
    start: Tuple[float, float] = Field(
        ..., description="Start Mars coordinates [x, y]"
    )
    goal: Tuple[float, float] = Field(
        ..., description="Goal Mars coordinates [x, y]"
    )
    mode: Literal["fastest", "balanced", "conservative"] = "balanced"


class RouteProperties(BaseModel):
    mode: str
    distance_km: float
    raw_terrain_cost: float
    average_raw_terrain_cost: float
    optimization_cost: float
    average_optimization_cost: float


class GeoJSONGeometry(BaseModel):
    type: Literal["LineString"] = "LineString"
    coordinates: List[Tuple[float, float]]


class GeoJSONFeature(BaseModel):
    type: Literal["Feature"] = "Feature"
    properties: RouteProperties
    geometry: GeoJSONGeometry


class GeoJSONResponse(BaseModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: List[GeoJSONFeature]
