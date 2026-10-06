def test_public_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_internal_health_no_key(client):
    response = client.get("/internal/health")
    assert response.status_code == 401
