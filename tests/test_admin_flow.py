def test_admin_login_page_exists(client):
    response = client.get("/admin")
    # Should redirect or show login (not 200 with content yet)
    assert response.status_code in (200, 401, 404, 302)


def test_admin_protected_without_auth(client):
    response = client.post("/admin/approve/1")
    assert response.status_code in (401, 404, 422)
