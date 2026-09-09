import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import auth, main


class DatabaseTestCase(unittest.TestCase):
    """Exercise HTTP handlers with isolated SQLite and no external services."""

    def setUp(self):
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        self.addCleanup(engine.dispose)
        auth.Base.metadata.create_all(engine)
        patcher = patch.object(auth, "SessionLocal", sessionmaker(bind=engine))
        patcher.start()
        self.addCleanup(patcher.stop)
        auth.create_user("operator", "password", "admin")
        self.client = TestClient(self.create_app())
        self.addCleanup(self.client.close)
        self.login()

    def create_app(self):
        return main.app

    def login(self, username="operator", password="password"):
        response = self.client.post("/api/auth/login", json={"username": username, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        return response
