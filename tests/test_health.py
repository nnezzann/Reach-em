from fastapi.testclient import TestClient

from reach_bot.app import api


def test_health() -> None:
    response = TestClient(api).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
