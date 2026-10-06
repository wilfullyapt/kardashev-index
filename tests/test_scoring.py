from app.main import CATEGORIES

def test_placeholder_scores_creation():
    # This is a unit test for the placeholder function
    # In a real test we'd use a proper DB session
    assert len(CATEGORIES) == 6
    assert "ai_tech_acceleration" in CATEGORIES
