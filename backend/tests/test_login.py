from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main as api


def test_login_and_register_normalize_email(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'login-test.db'}")
    api.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)

    def test_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    api.app.dependency_overrides[api.db] = test_db
    try:
        client = TestClient(api.app)
        registered = client.post(
            "/api/register",
            json={"name": "  Test User  ", "email": "  User@Example.Test  ", "password": "password123"},
        )
        assert registered.status_code == 200, registered.text
        assert registered.json()["user"]["email"] == "user@example.test"
        assert registered.json()["user"]["name"] == "Test User"

        logged_in = client.post(
            "/api/login",
            json={"email": " USER@EXAMPLE.TEST ", "password": "password123"},
        )
        assert logged_in.status_code == 200, logged_in.text
        assert logged_in.json()["user"]["email"] == "user@example.test"

        duplicate = client.post(
            "/api/register",
            json={"name": "Duplicate", "email": "user@example.test", "password": "password123"},
        )
        assert duplicate.status_code == 409
    finally:
        api.app.dependency_overrides.pop(api.db, None)
