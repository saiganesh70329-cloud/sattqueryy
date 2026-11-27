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

        invalid = client.post(
            "/api/register",
            json={"name": "Invalid", "email": "invalid@example.test", "password": "short"},
        )
        assert invalid.status_code == 400
        assert invalid.json()["detail"] == "Password must contain at least 8 characters"

        malformed = client.post("/api/register", json={"email": "invalid@example.test"})
        assert malformed.status_code == 422
        assert isinstance(malformed.json()["detail"], list)
        assert all("input" not in error for error in malformed.json()["detail"])

        duplicate = client.post(
            "/api/register",
            json={"name": "Duplicate", "email": "user@example.test", "password": "password123"},
        )
        assert duplicate.status_code == 409
    finally:
        api.app.dependency_overrides.pop(api.db, None)


def test_registration_database_error_is_safe_json(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'readonly-login-test.db'}")
    api.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)

    def readonly_db():
        session = Session()
        try:
            session.connection().exec_driver_sql("PRAGMA query_only=ON")
            yield session
        finally:
            session.close()

    api.app.dependency_overrides[api.db] = readonly_db
    try:
        client = TestClient(api.app, raise_server_exceptions=False)
        response = client.post(
            "/api/register",
            json={"name": "Test User", "email": "readonly@example.test", "password": "password123"},
        )
        assert response.status_code == 503
        assert response.headers["content-type"].startswith("application/json")
        assert response.json() == {
            "detail": "Account storage is temporarily unavailable. Please try again later."
        }
        assert "sqlite" not in response.text.lower()
        assert "traceback" not in response.text.lower()
    finally:
        api.app.dependency_overrides.pop(api.db, None)
