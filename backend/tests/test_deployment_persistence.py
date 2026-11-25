import hashlib
import importlib.util
import io
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app import main as api

MIGRATION_PATH = ROOT / "backend" / "scripts" / "migrate_sqlite_to_postgres.py"
spec = importlib.util.spec_from_file_location("sqlite_migration", MIGRATION_PATH)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def png_bytes(color):
    buffer = io.BytesIO()
    Image.fromarray(np.full((64, 64, 3), color, dtype=np.uint8)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_migration_copies_records_without_changing_sqlite_source(tmp_path):
    source = tmp_path / "legacy.db"
    target = tmp_path / "postgres-test-double.db"
    source_engine = create_engine(f"sqlite:///{source}")
    migration.METADATA.create_all(source_engine)
    created = datetime(2025, 3, 2, 4, 5, 6)
    result = json.dumps({"image": "data:image/png;base64,YWJj", "classes": [{"class": "Forest", "percent": 50}]})
    with source_engine.begin() as connection:
        connection.execute(migration.USERS.insert(), [{"id": 73, "email": "user@example.test", "name": "Test User", "password": "$2b$12$hash", "role": "admin", "active": True, "created": created}])
        connection.execute(migration.ANALYSES.insert(), [{"id": "analysis-id", "user_id": 73, "kind": "land", "name": "Test", "location": "", "lat": "", "lon": "", "source": "USER UPLOAD", "result": result, "created": created}])
        connection.execute(migration.NOTIFICATIONS.insert(), [{"id": 119, "user_id": 73, "message": "Analysis complete", "created": created}])
        connection.execute(migration.ORIGINAL_IMAGES.insert(), [{"id": 211, "analysis_id": "analysis-id", "role": "original", "filename": "canopy.png", "content_type": "image/png", "data": b"raw-png-data", "created": created}])
    source_engine.dispose()
    original_digest = hashlib.sha256(source.read_bytes()).digest()

    source_rows = migration.read_source(source)
    target_engine = create_engine(f"sqlite:///{target}")
    assert migration.migrate_records(source_rows, target_engine) == {"users": 1, "analyses": 1, "notifications": 1, "original_images": 1}
    with target_engine.connect() as connection:
        user = connection.execute(migration.select(migration.USERS)).mappings().one()
        analysis = connection.execute(migration.select(migration.ANALYSES)).mappings().one()
        notification = connection.execute(migration.select(migration.NOTIFICATIONS)).mappings().one()
        original_image = connection.execute(migration.select(migration.ORIGINAL_IMAGES)).mappings().one()
    assert (user["id"], user["password"], user["role"], user["created"]) == (73, "$2b$12$hash", "admin", created)
    assert (analysis["id"], analysis["user_id"], analysis["result"], analysis["created"]) == ("analysis-id", 73, result, created)
    assert (notification["id"], notification["user_id"], notification["created"]) == (119, 73, created)
    assert (original_image["id"], original_image["analysis_id"], original_image["data"], original_image["created"]) == (211, "analysis-id", b"raw-png-data", created)
    assert hashlib.sha256(source.read_bytes()).digest() == original_digest
    with pytest.raises(ValueError, match="not empty"):
        migration.migrate_records(source_rows, target_engine)
    target_engine.dispose()


def test_original_upload_is_durable_owner_scoped_and_reportable(tmp_path):
    db_path = tmp_path / "api-test.db"
    test_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    api.Base.metadata.create_all(test_engine)
    TestSession = sessionmaker(bind=test_engine, autoflush=False)
    with TestSession() as session:
        user = api.User(id=901, email="owner@example.test", name="Owner", password="unused", role="user", active=True)
        other = api.User(id=902, email="other@example.test", name="Other", password="unused", role="user", active=True)
        session.add_all([user, other])
        session.commit()
        owner_token = api.token(user)
        other_token = api.token(other)

    def test_db():
        session = TestSession()
        try:
            yield session
        finally:
            session.close()

    api.app.dependency_overrides[api.db] = test_db
    image_data = png_bytes((30, 110, 55))
    try:
        client = TestClient(api.app)
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        preflight = client.options(
            "/api/analysis/example/images/original/original",
            headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization"},
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == "http://localhost:5173"
        response = client.post(
            "/api/analyze/land",
            headers={"Authorization": f"Bearer {owner_token}"},
            data={"name": "Persistence check", "location": "Test", "source": "USER UPLOAD"},
            files={"file": ("canopy.png", image_data, "image/png")},
        )
        assert response.status_code == 200, response.text
        record = response.json()
        assert record["result"]["original_uploads_available"] is True
        analysis_id = record["id"]

        image_url = f"/api/analysis/{analysis_id}/images/original/original"
        fetched = client.get(image_url, headers={"Authorization": f"Bearer {owner_token}"})
        assert fetched.status_code == 200
        assert fetched.content == image_data
        assert fetched.headers["content-type"] == "image/png"
        assert "canopy.png" in fetched.headers["content-disposition"]
        assert client.get(image_url).status_code == 401
        assert client.get(image_url, headers={"Authorization": f"Bearer {other_token}"}).status_code == 404

        later_data = png_bytes((35, 115, 60))
        change = client.post(
            "/api/analyze/change",
            headers={"Authorization": f"Bearer {owner_token}"},
            data={"name": "Paired image persistence", "source": "USER UPLOAD"},
            files={
                "earlier": ("before.png", image_data, "image/png"),
                "later": ("after.png", later_data, "image/png"),
            },
        )
        assert change.status_code == 200, change.text
        for role, expected in (("earlier", image_data), ("later", later_data)):
            stored_upload = client.get(
                f"/api/analysis/{change.json()['id']}/images/{role}/original",
                headers={"Authorization": f"Bearer {owner_token}"},
            )
            assert stored_upload.status_code == 200
            assert stored_upload.content == expected

        flood = client.post(
            "/api/analyze/flood",
            headers={"Authorization": f"Bearer {owner_token}"},
            data={"name": "Flood image persistence", "source": "USER UPLOAD"},
            files={"file": ("flood.png", image_data, "image/png")},
        )
        assert flood.status_code == 200, flood.text
        stored_flood = client.get(
            f"/api/analysis/{flood.json()['id']}/images/original/original",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert stored_flood.status_code == 200
        assert stored_flood.content == image_data

        flood_pair = client.post(
            "/api/analyze/flood",
            headers={"Authorization": f"Bearer {owner_token}"},
            data={"name": "Flood comparison persistence", "source": "USER UPLOAD"},
            files={
                "before_file": ("before-flood.png", image_data, "image/png"),
                "after_file": ("after-flood.png", later_data, "image/png"),
            },
        )
        assert flood_pair.status_code == 200, flood_pair.text
        for role, expected in (("before", image_data), ("after", later_data)):
            stored_upload = client.get(
                f"/api/analysis/{flood_pair.json()['id']}/images/{role}/original",
                headers={"Authorization": f"Bearer {owner_token}"},
            )
            assert stored_upload.status_code == 200
            assert stored_upload.content == expected

        report = client.get(f"/api/analysis/{analysis_id}/report", headers={"Authorization": f"Bearer {owner_token}"})
        assert report.status_code == 200
        assert report.content.startswith(b"%PDF-")
    finally:
        api.app.dependency_overrides.pop(api.db, None)

    # Re-open the database after closing the request session to verify persistence.
    with TestSession() as session:
        stored = session.query(api.OriginalImage).filter_by(analysis_id=analysis_id, role="original").one()
        assert stored.data == image_data
    test_engine.dispose()
