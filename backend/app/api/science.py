from fastapi import APIRouter, HTTPException

from app.core.science.crism.loader import load_crism_targets
from app.schemas.science import ScienceTargetsResponse


router = APIRouter(
    prefix="/api/v1/science",
    tags=["Science"],
)


@router.get(
    "/targets",
    response_model=ScienceTargetsResponse,
)
def get_science_targets():

    try:
        registry = load_crism_targets()
        targets = registry.get_all()

        return {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "id": target.id,
                        "name": target.name,
                        "target_type": target.target_type,
                        "source": target.source,
                        "science_value": target.science_value,
                        "ranking_method": target.ranking_method,
                        "evidence": [
                            {
                                "parameter": item.parameter,
                                "value_dn": item.value_dn,
                                "threshold_dn": item.threshold_dn,
                                "relative_percentile": item.relative_percentile,
                                "passed_threshold": item.passed_threshold,
                            }
                            for item in target.evidence
                        ],
                    },
                    "geometry": {
                        "type": "Point",
                        "coordinates": [target.x, target.y],
                    },
                }
                for target in targets
            ],
        }

    except FileNotFoundError:
        raise HTTPException(
            status_code=503,
            detail="CRISM science data is unavailable.",
        )