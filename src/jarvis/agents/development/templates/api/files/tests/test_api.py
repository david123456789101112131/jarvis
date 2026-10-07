from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.storage import JsonStore


@pytest.fixture
def client(tmp_path):
    settings = Settings(title="Test API", description="tests", lang="en", data_file=tmp_path / "items.json")
    return TestClient(create_app(settings, JsonStore(settings.data_file)))


def test_health(client):
    assert client.get("/health").json() == {"status": "ok", "items": 0}


def test_create_read_update_delete(client):
    created = client.post("/items", json={"name": " Notebook ", "price": 12.5, "tags": ["Paper", "paper", "office"]})
    assert created.status_code == 201
    item = created.json()
    assert item["name"] == "Notebook"
    assert item["tags"] == ["paper", "office"]
    item_id = item["id"]
    assert client.get(f"/items/{item_id}").json()["price"] == 12.5
    patched = client.patch(f"/items/{item_id}", json={"done": True})
    assert patched.json()["done"] is True and patched.json()["name"] == "Notebook"
    replaced = client.put(f"/items/{item_id}", json={"name": "Pen"})
    assert replaced.json()["name"] == "Pen" and replaced.json()["price"] == 0
    assert client.delete(f"/items/{item_id}").status_code == 204
    assert client.get(f"/items/{item_id}").status_code == 404
    assert client.delete(f"/items/{item_id}").status_code == 404


def test_validation_errors(client):
    assert client.post("/items", json={"name": ""}).status_code == 422
    assert client.post("/items", json={"name": "x", "price": -1}).status_code == 422
    assert client.patch("/items/999", json={"done": True}).status_code == 404


def test_search_filter_and_paging(client):
    for i in range(5):
        client.post("/items", json={"name": f"Item {i}", "description": "blue" if i % 2 else "red", "tags": ["t"]})
    client.patch("/items/1", json={"done": True})
    page = client.get("/items", params={"limit": 2, "offset": 1}).json()
    assert page["total"] == 5 and len(page["items"]) == 2 and page["items"][0]["id"] == 2
    assert client.get("/items", params={"q": "blue"}).json()["total"] == 2
    assert client.get("/items", params={"tag": "T"}).json()["total"] == 5
    assert client.get("/items", params={"done": True}).json()["total"] == 1


def test_data_survives_restart(tmp_path):
    settings = Settings(title="T", description="d", lang="en", data_file=tmp_path / "items.json")
    TestClient(create_app(settings, JsonStore(settings.data_file))).post("/items", json={"name": "kept"})
    again = TestClient(create_app(settings, JsonStore(settings.data_file)))
    assert again.get("/items").json()["items"][0]["name"] == "kept"
    assert again.post("/items", json={"name": "next"}).json()["id"] == 2
