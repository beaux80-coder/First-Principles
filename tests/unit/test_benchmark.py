"""Tests for the benchmark API and calculation engine."""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock


@pytest.fixture
def client():
    """Create a test client with mocked database."""
    from app.main import app
    from app.database import get_db

    mock_db = MagicMock()
    # Mock the price data count query
    mock_db.query.return_value.filter.return_value.scalar.return_value = 0
    mock_db.add = MagicMock()
    mock_db.commit = MagicMock()
    mock_db.refresh = MagicMock()

    # Make refresh set query_id on the object
    def fake_refresh(obj):
        import uuid
        obj.query_id = uuid.uuid4()
    mock_db.refresh.side_effect = fake_refresh

    app.dependency_overrides[get_db] = lambda: mock_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_benchmark_post(client):
    response = client.post("/api/v1/benchmark/", json={
        "employee_count": 200,
        "state": "CA",
        "industry": "Technology",
        "annual_spend": 1800000,
    })
    assert response.status_code == 200
    data = response.json()

    # Verify structure
    assert "query_id" in data
    assert "current_cost" in data
    assert "system_cost" in data
    assert "comparison" in data
    assert "experience_comparison" in data
    assert "transparency" in data
    assert "share_url" in data

    # Verify savings are positive
    assert data["comparison"]["annual_savings"] > 0
    assert data["comparison"]["savings_pct"] > 0

    # Verify system cost is lower than current cost
    assert data["system_cost"]["total_annual"] < data["current_cost"]["total_annual"]

    # Verify experience comparison shows the full product difference
    assert data["experience_comparison"]["system"]["employee_cost_sharing"] is False
    assert data["experience_comparison"]["system"]["avg_employee_oop_per_year"] == 0
    assert data["experience_comparison"]["system"]["employee_scheduling_required"] is False

    # Verify transparency info is present
    assert "current" in data["transparency"]
    assert "system" in data["transparency"]


def test_benchmark_no_spend(client):
    """Benchmark should work even without annual_spend (uses national avg)."""
    response = client.post("/api/v1/benchmark/", json={
        "employee_count": 50,
        "state": "TX",
        "industry": "Retail",
        "annual_spend": None,
    })
    assert response.status_code == 200
    data = response.json()
    assert data["comparison"]["annual_savings"] > 0


def test_benchmark_validation(client):
    """Invalid inputs should be rejected."""
    response = client.post("/api/v1/benchmark/", json={
        "employee_count": 0,
        "state": "CA",
        "industry": "Tech",
    })
    assert response.status_code == 422
