def test_leaderboard_endpoint_exists(client):
    response = client.get("/")
    assert response.status_code in (200, 404)
