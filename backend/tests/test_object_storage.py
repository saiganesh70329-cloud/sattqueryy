import io

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main as api


class FakeObjectStore:
    def __init__(self):
        self.objects = {}

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        assert operation == "put_object"
        assert ExpiresIn == 300
        return f"https://objects.example.test/{Params['Key']}?signature=test-only"

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = (bytes(Body), ContentType)

    def get_object(self, Bucket, Key):
        body, content_type = self.objects[Key]
        return {"Body": io.BytesIO(body), "ContentLength": len(body), "ContentType": content_type}


def test_direct_upload_processes_and_retrieves_private_assets(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'object-storage-test.db'}")
    api.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    with Session() as session:
        owner = api.User(id=1001, email="owner@example.test", name="Owner", password="unused", role="user", active=True)
        other = api.User(id=1002, email="other@example.test", name="Other", password="unused", role="user", active=True)
        session.add_all([owner, other])
        session.commit()
        owner_token = api.token(owner)
        other_token = api.token(other)

    store = FakeObjectStore()
    monkeypatch.setattr(api, "OBJECT_STORAGE_ENABLED", True)
    monkeypatch.setattr(api, "OBJECT_STORAGE_CONFIG", {
        "endpoint_url": "https://objects.example.test",
        "bucket": "private-tests",
        "region": "test-region",
        "access_key_id": "test-only",
        "secret_access_key": "test-only",
    })
    monkeypatch.setattr(api, "object_store", lambda: store)
    monkeypatch.setattr(api, "APP_ENV", "production")

    def test_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    api.app.dependency_overrides[api.db] = test_db
    try:
        client = TestClient(api.app)
        assert client.get("/api/uploads/config").json() == {"direct_uploads": True}
        ticket = client.post(
            "/api/uploads/presign",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"filename": "canopy.png", "content_type": "image/png", "size": 256},
        )
        assert ticket.status_code == 200
        key = ticket.json()["object_key"]
        assert key.startswith("uploads/1001/")

        image_buffer = io.BytesIO()
        Image.fromarray(np.full((64, 64, 3), (30, 110, 55), dtype=np.uint8)).save(image_buffer, format="PNG")
        original = image_buffer.getvalue()
        store.put_object("private-tests", key, original, "image/png")
        analysis = client.post(
            "/api/analyze/land",
            headers={"Authorization": f"Bearer {owner_token}"},
            data={"name": "Direct upload", "file_upload_key": key, "file_upload_filename": "canopy.png"},
        )
        assert analysis.status_code == 200, analysis.text
        analysis_id = analysis.json()["id"]

        original_response = client.get(
            f"/api/analysis/{analysis_id}/images/original/original",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert original_response.status_code == 200
        assert original_response.content == original
        assert client.get(
            f"/api/analysis/{analysis_id}/images/original/original",
            headers={"Authorization": f"Bearer {other_token}"},
        ).status_code == 404

        preview_url = analysis.json()["result"]["image"]
        assert preview_url.startswith(f"/api/analysis/{analysis_id}/previews/image?token=")
        preview = client.get(preview_url)
        assert preview.status_code == 200
        assert preview.headers["content-type"] == "image/png"
        assert preview.content

        history = client.get("/api/history", headers={"Authorization": f"Bearer {owner_token}"})
        assert history.status_code == 200
        assert history.json()[0]["result"]["image"].startswith("/api/analysis/")
        assert "data:image" not in history.text

        report = client.get(f"/api/analysis/{analysis_id}/report", headers={"Authorization": f"Bearer {owner_token}"})
        assert report.status_code == 200
        assert report.content.startswith(b"%PDF")

        forged = client.post(
            "/api/analyze/land",
            headers={"Authorization": f"Bearer {other_token}"},
            data={"name": "Cross-user reference", "file_upload_key": key, "file_upload_filename": "canopy.png"},
        )
        assert forged.status_code == 403
    finally:
        api.app.dependency_overrides.pop(api.db, None)
        engine.dispose()
