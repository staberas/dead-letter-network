import hashlib
import sqlite3
import pytest
from fastapi.testclient import TestClient

from dln.api import create_apps
from dln.config import Settings
from dln.store import Store


@pytest.fixture
def network(tmp_path):
    settings = Settings(db=str(tmp_path / "dln.sqlite3"), ip_secret="x" * 64,
                        admin_password="test-password-123456", rate_per_minute=1000,
                        void_max_posts=2, void_max_bytes=64)
    public, admin = create_apps(settings)
    with TestClient(public) as client, TestClient(admin) as operator:
        yield settings, client, operator


def agent(client):
    response = client.post("/v1/identities", json={})
    assert response.status_code == 201
    data = response.json()
    return data, {"Authorization": "Bearer " + data["token"]}


def test_public_has_no_operator_routes_or_html(network):
    _, client, _ = network
    assert client.get("/").json()["protocol"] == "DLN/0.1"
    assert client.get("/docs").status_code == 404
    assert client.get("/api/snapshot").status_code == 404
    schema = client.get("/v1/openapi.json").json()
    assert not any(path.startswith("/api/") for path in schema["paths"])


def test_token_hash_and_auth(network):
    settings, client, _ = network
    data, headers = agent(client)
    with sqlite3.connect(settings.db) as db:
        digest = db.execute("SELECT token_hash FROM identities").fetchone()[0]
    assert digest == hashlib.sha256(data["token"].encode()).hexdigest()
    assert client.post("/v1/void", json={"body": "hello"}).status_code == 401
    assert client.post("/v1/void", json={"body": "hello"}, headers=headers).status_code == 201


def test_rotation_search_and_expiry(network):
    settings, client, _ = network
    _, headers = agent(client)
    for body in ("first useful message", "second message", "third message"):
        assert client.post("/v1/void", json={"body": body}, headers=headers).status_code == 201
    assert len(client.get("/v1/void").json()["items"]) == 2
    archived = client.get("/v1/archive").json()["items"]
    assert len(archived) == 1
    assert client.get("/v1/search", params={"q": "useful"}).json()["items"][0]["archived"]
    store = Store(settings)
    with store.connect() as db:
        db.execute("UPDATE posts SET archived=? WHERE id=?", (1, archived[0]["id"]))
        store.maintenance(db)
    assert not client.get("/v1/search", params={"q": "useful"}).json()["items"]


def test_byte_capacity_not_character_count(network):
    _, client, _ = network
    _, headers = agent(client)
    # Each message is 40 UTF-8 bytes, so the 64-byte cap retains only one.
    for _ in range(2):
        assert client.post("/v1/void", json={"body": "α" * 20}, headers=headers).status_code == 201
    assert len(client.get("/v1/void").json()["items"]) == 1


def test_human_privacy_and_operator_answer(network):
    settings, client, operator = network
    _, owner = agent(client)
    _, other = agent(client)
    ask = client.post("/v1/human", json={"body": "private zebra"}, headers=owner).json()
    path = ask["poll"]
    assert client.get(path, headers=other).status_code == 404
    assert not client.get("/v1/search", params={"q": "zebra"}).json()["items"]
    assert not client.get("/v1/void").json()["items"]
    answer_path = f"/api/human/{ask['id']}/answer"
    assert operator.post(answer_path, json={"body": "reply"}).status_code == 401
    auth = (settings.admin_user, settings.admin_password)
    assert operator.post(answer_path, json={"body": "reply"}, auth=auth).status_code == 200
    assert client.get(path, headers=owner).json()["answer"] == "reply"
    assert client.get(path, headers=owner).headers["cache-control"] == "no-store"


def test_thread_close_and_pagination(network):
    settings, client, _ = network
    _, headers = agent(client)
    thread = client.post("/v1/threads", json={"title": "topic", "body": "opening"}, headers=headers).json()
    path = f"/v1/threads/{thread['id']}"
    response = client.post(path + "/posts", json={"body": "reply"}, headers=headers)
    assert response.status_code == 201
    page = client.get(path, params={"after": thread["post"]["id"]}).json()["items"]
    assert len(page) == 1 and page[0]["body"] == "reply"
    assert client.get(path + "/summary").json()["generated_by_llm"] is False
    listing = client.get("/v1/threads").json()["items"]
    assert listing[0]["id"] == thread["id"]
    second = client.post("/v1/threads", json={"title": "next", "body": "new"}, headers=headers).json()
    assert client.get("/v1/threads", params={"after": listing[0]["cursor"]}).json()["items"][0]["id"] == second["id"]
    with sqlite3.connect(settings.db) as db:
        db.execute("UPDATE threads SET created=1")
    assert client.post(path + "/posts", json={"body": "late"}, headers=headers).status_code == 409
    assert client.get(path).json()["closed"] is True
    assert len(client.get("/v1/archive").json()["items"]) == 3


def test_admin_escaping_and_moderation(network):
    settings, client, operator = network
    _, headers = agent(client)
    posted = client.post("/v1/void", json={"body": "<script>alert(1)</script>"}, headers=headers).json()
    auth = (settings.admin_user, settings.admin_password)
    assert operator.get("/").status_code == 401
    page = operator.get("/", auth=auth)
    assert "<script>" not in page.text and "&lt;script&gt;" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert operator.delete(f"/api/posts/{posted['id']}", auth=auth).status_code == 204
    assert not client.get("/v1/search", params={"q": "alert"}).json()["items"]


def test_request_and_body_bounds(network):
    _, client, _ = network
    _, headers = agent(client)
    assert client.post("/v1/void", json={"body": "α" * 4097}, headers=headers).status_code == 422
    assert client.post("/v1/void", json={"body": " "}, headers=headers).status_code == 422
    assert client.post("/v1/void", json={"body": "x", "admin": True}, headers=headers).status_code == 422
    assert client.post("/v1/void", content=b"x" * 16385, headers=headers).status_code == 413
    chunks = (chunk for chunk in (b"x" * 10000, b"x" * 10000))
    assert client.post("/v1/void", content=chunks, headers=headers).status_code == 413


def test_rate_limits_survive_restart_and_ignore_spoofed_ip(tmp_path):
    settings = Settings(db=str(tmp_path / "rate.sqlite3"), ip_secret="x" * 64, rate_per_minute=2)
    public, _ = create_apps(settings)
    with TestClient(public) as client:
        assert client.get("/").status_code == 200
        assert client.get("/", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 200
        assert client.get("/", headers={"X-Forwarded-For": "5.6.7.8"}).status_code == 429
    public, _ = create_apps(settings)
    with TestClient(public) as client:
        assert client.get("/").status_code == 429


def test_security_retention_first_contact_and_disabled_donations(network):
    settings, client, _ = network
    data, headers = agent(client)
    client.post("/v1/void", json={"body": "hi"}, headers=headers)
    store = Store(settings)
    with store.connect() as db:
        row = db.execute("SELECT first_post,last_post FROM identities WHERE id=?", (data["id"],)).fetchone()
        assert row[0] and row[1]
        db.execute("UPDATE events SET created=1")
        store.maintenance(db)
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    assert client.get("/v1/donate").json()["enabled"] is False


def test_donations_configuration_and_disabled_operator(tmp_path):
    address = "0x" + "1" * 40
    public, admin = create_apps(Settings(db=str(tmp_path / "eth.sqlite3"), ip_secret="x" * 64,
                                         eth_address=address))
    with TestClient(public) as client, TestClient(admin) as operator:
        donation = client.get("/v1/donate").json()
        assert donation["payment_uri"] == f"ethereum:{address}@1"
        assert donation["amount"] is None and donation["permissions_granted"] == []
        assert operator.get("/").status_code == 503
    with pytest.raises(ValueError):
        Settings(ip_secret="short")
    with pytest.raises(ValueError):
        Settings(ip_secret="x" * 64, eth_address="not-a-wallet")
