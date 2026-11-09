from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_science_targets_endpoint():

    response = client.get("/api/v1/science/targets")

    assert response.status_code == 200

    data = response.json()

    assert data["type"] == "FeatureCollection"
    assert len(data["features"]) == 295


def test_science_target_geojson_structure():

    response = client.get("/api/v1/science/targets")

    feature = response.json()["features"][0]

    assert feature["type"] == "Feature"

    assert feature["geometry"]["type"] == "Point"

    assert len(feature["geometry"]["coordinates"]) == 2

    assert feature["properties"]["source"] == "CRISM"


def test_science_values_are_normalized():

    response = client.get("/api/v1/science/targets")

    features = response.json()["features"]

    for feature in features:

        score = feature["properties"]["science_value"]

        assert 0 <= score <= 1


def test_science_targets_have_unique_ids():

    response = client.get("/api/v1/science/targets")

    features = response.json()["features"]

    ids = [
        feature["properties"]["id"]
        for feature in features
    ]

    assert len(ids) == len(set(ids))
def test_science_targets_include_spectral_evidence():

    response = client.get("/api/v1/science/targets")

    assert response.status_code == 200

    features = response.json()["features"]

    for feature in features:
        properties = feature["properties"]

        evidence = properties["evidence"]

        assert len(evidence) == 2

        parameters = {
            item["parameter"]
            for item in evidence
        }

        assert parameters == {"D2300", "BD1900_2"}

        for item in evidence:
            assert item["passed_threshold"] is True
            assert item["value_dn"] >= item["threshold_dn"]


def test_science_value_matches_evidence_ranking():

    response = client.get("/api/v1/science/targets")

    features = response.json()["features"]

    for feature in features:
        properties = feature["properties"]

        evidence = properties["evidence"]

        expected_score = sum(
            item["relative_percentile"]
            for item in evidence
        ) / 2

        assert abs(
            properties["science_value"] - expected_score
        ) < 1e-9

        assert (
            properties["ranking_method"]
            == "mean_relative_percentile"
        )    