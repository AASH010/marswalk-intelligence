import rasterio

from fastapi import APIRouter, HTTPException, status

from app.schemas.route import RouteRequest, GeoJSONResponse
from app.core.terrain.geotiff import load_terrain, grid_to_map
from app.services.route_service import calculate_route


router = APIRouter()


@router.post(
    "/navigate",
    response_model=GeoJSONResponse,
    status_code=status.HTTP_200_OK,
)
def navigate(request: RouteRequest):

    terrain = load_terrain()

    terrain_cost = terrain["terrain_cost"]
    blocked_mask = terrain["blocked_mask"]
    resolution = terrain["resolution"]
    transform = terrain["transform"]

    rows, cols = terrain_cost.shape

    # Convert projected Mars coordinates to raster grid indices.
    start_x, start_y = request.start
    goal_x, goal_y = request.goal

    start_row, start_col = rasterio.transform.rowcol(
        transform, start_x, start_y
    )

    goal_row, goal_col = rasterio.transform.rowcol(
        transform, goal_x, goal_y
    )

    start = (int(start_row), int(start_col))
    goal = (int(goal_row), int(goal_col))

    # Validate raster boundaries.
    for name, (row, col) in [
        ("Start", start),
        ("Goal", goal),
    ]:
        if not (0 <= row < rows and 0 <= col < cols):
            raise HTTPException(
                status_code=400,
                detail=f"{name} coordinates outside terrain raster."
            )

    # Calculate route.
    try:
        result = calculate_route(
            terrain_cost=terrain_cost,
            blocked_mask=blocked_mask,
            start=start,
            goal=goal,
            resolution=resolution,
            mode_name=request.mode,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        )

    if result is None:
        raise HTTPException(
            status_code=404,
            detail="No valid route found."
        )

    # Convert resulting grid path to projected Mars coordinates.
    gis_path = [
        grid_to_map(row, col, transform)
        for row, col in result["path"]
    ]

    feature = {
        "type": "Feature",
        "properties": {
            "mode": result["mode"],
            "distance_km": round(result["distance_km"], 3),
            "raw_terrain_cost": round(result["raw_terrain_cost"], 2),
            "average_raw_terrain_cost": round(
                result["average_raw_terrain_cost"], 4
            ),
            "optimization_cost": round(
                result["optimization_cost"], 2
            ),
            "average_optimization_cost": round(
                result["average_optimization_cost"], 4
            ),
        },
        "geometry": {
            "type": "LineString",
            "coordinates": gis_path,
        },
    }

    return {
        "type": "FeatureCollection",
        "features": [feature],
    }
    