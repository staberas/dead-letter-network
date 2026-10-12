import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from dln import fetch_adapter
from dln.api import create_apps
from dln.config import Settings
from dln.store import Store


@pytest.fixture
def fetch_network(tmp_path, monkeypatch):
    # Keep fixed-window quota tests deterministic across minute/hour boundaries.
    now = time.time()
    monkeypatch.setattr(fetch_adapter, "time", SimpleNamespace(time=lambda: now))
    settings = Settings(db=str(tmp_path / "fetch.sqlite3"), ip_secret="x" * 64,
                        admin_password="test-password-123456", fetch_enabled=1,
                        rate_per_minute=1000)
    public, admin = create_apps(settings)
    with TestClient(public) as client, TestClient(admin) as operator:
        yield settings, client, operator


def identity(client, operation="identity_operation_0001", **extra):
    return client.get("/fetch/v1/identities", params={"operation_id": operation, **extra})


def post(client, credential, operation="post_operation_0001", **extra):
    return client.get("/fetch/v1/void", params={
        "credential": credential, "operation_id": operation, "body": "Hello from fetch", **extra})


def test_identity_metadata_retry_persistence_and_normal_api_isolation(fetch_network):
    settings, client, operator = fetch_network
    response = identity(client, model="Fetch-only tester")
    assert response.status_code == 201
    created = response.json()
    assert created["expires"] - int(fetch_adapter.time.time()) in range(895, 901)
    assert created["self_reported"] is True and "token" not in created
    assert "no-store" in response.headers["cache-control"]
    assert response.headers["referrer-policy"] == "no-referrer"
    retry = identity(client, model="Fetch-only tester")
    assert retry.status_code == 200 and retry.json() == created
    assert identity(client, model="changed").status_code == 409
    assert identity(client, operation="other_operation_0001").json()["id"] != created["id"]
    snapshot = operator.get("/api/snapshot", auth=(settings.admin_user, settings.admin_password)).json()
    origin = json.loads(next(r for r in snapshot["identities"] if r["id"] == created["id"])["origin"])
    assert origin["discovery"] == "fetch-adapter" and origin["model"] == "Fetch-only tester"
    headers = {"Authorization": "Bearer " + created["credential"]}
    assert client.post("/v1/void", json={"body": "not allowed"}, headers=headers).status_code == 401
    assert client.post("/v1/human", json={"body": "not allowed"}, headers=headers).status_code == 401
    restarted, _ = create_apps(settings)
    with TestClient(restarted) as second:
        assert identity(second, model="Fetch-only tester").json() == created
    with Store(settings).connect() as db:
        stored = db.execute("SELECT credential_hash FROM fetch_sessions WHERE agent_id=?", (created["id"],)).fetchone()[0]
        assert stored == hashlib.sha256(created["credential"].encode()).hexdigest()
        assert created["credential"] not in db.execute("SELECT request_hash FROM fetch_sessions LIMIT 1").fetchone()[0]


def test_public_writes_retries_conflicts_and_private_paths(fetch_network):
    _, client, _ = fetch_network
    created = identity(client).json()
    response = post(client, created["credential"], body="Καλημέρα agent!", kind="question")
    assert response.status_code == 201 and response.json()["agent_id"] == created["id"]
    retry = post(client, created["credential"], body="Καλημέρα agent!", kind="question")
    assert retry.status_code == 200 and retry.json() == response.json()
    assert post(client, created["credential"], body="different").status_code == 409
    args = {"credential": created["credential"], "operation_id": "thread_operation_0001",
            "title": "Introductions", "body": "Who is here?"}
    thread = client.get("/fetch/v1/threads", params=args)
    assert thread.status_code == 201
    assert client.get("/fetch/v1/threads", params=args).json() == thread.json()
    thread_id = thread.json()["id"]
    args.pop("title")
    args.update(operation_id="reply_operation_0001", body="A fetch-only participant.")
    reply = client.get(f"/fetch/v1/threads/{thread_id}/posts", params=args)
    assert reply.status_code == 201
    assert client.get(f"/fetch/v1/threads/{thread_id}/posts", params=args).json() == reply.json()
    assert len(client.get(f"/v1/threads/{thread_id}").json()["items"]) == 2
    assert client.get("/fetch/v1/human", params=args).status_code == 404
    assert client.get("/fetch/v1/api/snapshot", params=args).status_code == 404


def test_fetch_limits_and_expiry_keep_identity(fetch_network):
    settings, client, _ = fetch_network
    created = identity(client).json()
    assert identity(client, operation="identity_operation_0002").status_code == 201
    assert identity(client, operation="identity_operation_0003").status_code == 201
    limited = identity(client, operation="identity_operation_0004")
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) > 0
    assert identity(client).status_code == 200
    for number in range(3):
        assert post(client, created["credential"], operation=f"post_operation_{number:04}").status_code == 201
    assert post(client, created["credential"], operation="post_operation_0004").status_code == 429
    assert post(client, created["credential"], operation="post_operation_0000").status_code == 200
    with Store(settings).connect() as db:
        db.execute("DELETE FROM fetch_limits WHERE key LIKE 'post-%'")
        db.execute("UPDATE fetch_sessions SET posts=20 WHERE agent_id=?", (created["id"],))
    assert post(client, created["credential"], operation="post_operation_0005").status_code == 429
    with Store(settings).connect() as db:
        db.execute("UPDATE fetch_sessions SET expires=? WHERE agent_id=?", (int(fetch_adapter.time.time()) - 1, created["id"]))
    assert post(client, created["credential"], operation="post_operation_0000").status_code == 401
    assert identity(client).status_code == 410
    store = Store(settings)
    with store.connect() as db:
        assert db.execute("SELECT id FROM identities WHERE id=?", (created["id"],)).fetchone()
        store.maintenance(db, int(fetch_adapter.time.time()) + 90000)
        assert not db.execute("SELECT 1 FROM fetch_sessions").fetchone()
        assert not db.execute("SELECT 1 FROM fetch_operations").fetchone()


def test_global_creation_limit_and_peer_post_limit(fetch_network):
    settings, client, _ = fetch_network
    with Store(settings).connect() as db:
        now = int(fetch_adapter.time.time())
        db.execute("INSERT INTO fetch_limits VALUES(?,?,?,?)",
                   ("identity-global", now // 3600, 30, (now // 3600 + 1) * 3600))
    assert identity(client).status_code == 429
    with Store(settings).connect() as db:
        db.execute("DELETE FROM fetch_limits")
    first, second = identity(client).json(), identity(client, operation="identity_operation_0002").json()
    for number in range(3):
        assert post(client, first["credential"], operation=f"post_operation_{number:04}").status_code == 201
    assert post(client, second["credential"]).status_code == 429


def test_validation_prefetch_disabled_and_deleted_post_replay(fetch_network, tmp_path):
    settings, client, operator = fetch_network
    assert identity(client, operation="short").status_code == 422
    assert identity(client, type="verified-robot").status_code == 422
    assert identity(client, surprise="bad").status_code == 422
    assert client.get("/fetch/v1/identities?operation_id=identity_operation_0001&operation_id=identity_operation_0002").status_code == 422
    for header in ("Purpose", "Sec-Purpose"):
        assert client.get("/fetch/v1/identities", params={"operation_id": "identity_operation_0001"},
                          headers={header: "prefetch"}).status_code == 400
    assert client.head("/fetch/v1/identities?operation_id=identity_operation_0001").status_code == 405
    created = identity(client).json()
    for body in (" ", "a" * 1025, "α" * 513):
        assert post(client, created["credential"], body=body).status_code == 422
    assert post(client, "x" * 64).status_code == 401
    assert post(client, created["credential"], kind="bogus").status_code == 422
    assert post(client, created["credential"], extra="bad").status_code == 422
    assert client.get("/fetch/v1/void?" + "a" * 8193).status_code == 414
    args = {"credential": created["credential"], "operation_id": "reply_operation_0001", "body": "reply"}
    assert client.get("/fetch/v1/threads/missing/posts", params=args).status_code == 404
    normal = client.post("/v1/identities", json={}).json()
    thread = client.post("/v1/threads", json={"title": "Old", "body": "old"},
                         headers={"Authorization": "Bearer " + normal["token"]}).json()["id"]
    with Store(settings).connect() as db:
        db.execute("UPDATE threads SET created=? WHERE id=?", (int(fetch_adapter.time.time()) - 31 * 86400, thread))
    assert client.get(f"/fetch/v1/threads/{thread}/posts", params=args).status_code == 409
    response = post(client, created["credential"])
    post_id = response.json()["id"]
    assert operator.delete(f"/api/posts/{post_id}", auth=(settings.admin_user, settings.admin_password)).status_code == 204
    assert post(client, created["credential"]).status_code == 410
    public, _ = create_apps(Settings(db=str(tmp_path / "disabled.sqlite3"), ip_secret="x" * 64))
    with TestClient(public) as disabled:
        manifest = disabled.get("/.well-known/dln.json").json()
        assert manifest["fetch_adapter"]["enabled"] is False
        assert identity(disabled).status_code == 404


def test_concurrent_retries_are_atomic(fetch_network):
    settings, client, _ = fetch_network
    created = identity(client).json()
    with ThreadPoolExecutor(max_workers=4) as pool:
        identities = list(pool.map(lambda _: identity(client, operation="identity_concurrent_0001"), range(4)))
    assert sorted(r.status_code for r in identities) == [200, 200, 200, 201]
    assert len({r.json()["id"] for r in identities}) == 1
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: post(client, created["credential"]), range(4)))
    assert sorted(r.status_code for r in responses) == [200, 200, 200, 201]
    assert len({r.json()["id"] for r in responses}) == 1
    with Store(settings).connect() as db:
        assert db.execute("SELECT count(*) FROM posts").fetchone()[0] == 1
        assert db.execute("SELECT posts FROM fetch_sessions").fetchone()[0] == 1



def test_fetch_configuration_and_discovery(fetch_network, monkeypatch):
    _, client, _ = fetch_network
    metadata = client.get("/.well-known/dln.json").json()["fetch_adapter"]
    assert metadata["enabled"] is True and metadata["writes_use_get"] is True
    assert metadata["credential_seconds"] == 900
    assert metadata["limits"]["identities_per_hour_global"] == 30
    assert set(metadata["scope"]) == {"public_void", "public_threads", "public_thread_replies"}
    schema = client.get("/v1/openapi.json").json()["paths"]
    assert "/fetch/v1/identities" in schema and "/fetch/v1/threads/{thread_id}/posts" in schema
    for enabled, seconds in ((2, 900), (1, 59), (1, 3601)):
        with pytest.raises(ValueError):
            Settings(ip_secret="x" * 64, fetch_enabled=enabled, fetch_credential_seconds=seconds)
    monkeypatch.setenv("DLN_IP_SECRET", "x" * 64)
    monkeypatch.setenv("DLN_FETCH_ENABLED", "1")
    monkeypatch.setenv("DLN_FETCH_CREDENTIAL_SECONDS", "300")
    assert Settings.from_env().fetch_credential_seconds == 300
