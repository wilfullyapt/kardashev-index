def test_internal_health_with_invalid_key(client):
    response = client.get("/internal/health", headers={"X-Hermes-Key": "wrong-key"})
    assert response.status_code == 401


def test_internal_health_no_key(client):
    response = client.get("/internal/health")
    assert response.status_code == 401
