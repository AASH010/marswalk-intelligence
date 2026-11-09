from typing import List, Literal, Tuple
from pydantic import BaseModel


class SpectralEvidenceSchema(BaseModel):
    parameter: str
    value_dn: float
    threshold_dn: float
    relative_percentile: float
    passed_threshold: bool


class ScienceTargetProperties(BaseModel):
    id: str
    name: str
    target_type: str
    source: str
    science_value: float
    ranking_method: str
    evidence: List[SpectralEvidenceSchema]


class SciencePointGeometry(BaseModel):
    type: Literal["Point"] = "Point"
    coordinates: Tuple[float, float]


class ScienceTargetFeature(BaseModel):
    type: Literal["Feature"] = "Feature"
    properties: ScienceTargetProperties
    geometry: SciencePointGeometry


class ScienceTargetsResponse(BaseModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: List[ScienceTargetFeature]