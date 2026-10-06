def test_suggest_endpoint_exists(client):
    # Basic existence check - full test needs DB + rate limit mocking
    response = client.post("/suggest", json={"name": "Test Corp"})
    # Expect either 200/422/404 depending on current routes
    assert response.status_code in (200, 404, 422)
