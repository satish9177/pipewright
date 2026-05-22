"""
conftest.py
Pytest configuration for all backend tests.
Initializes database before test session starts.
"""

import pytest
from backend.db.database import init_db


@pytest.fixture(scope="session", autouse=True)
def initialize_database():
    init_db()
