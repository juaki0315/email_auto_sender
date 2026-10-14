import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from database import Database  # noqa: E402
from mock_sender import MockMailer  # noqa: E402

TOKEN = "token-de-prueba"


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.db")


@pytest.fixture
def mailer():
    return MockMailer("joaquin@agasesor.com", delay=0)


@pytest.fixture
def client(db, mailer):
    app = create_app(mailer, db, synchronous=True, csrf_token=TOKEN)
    app.config["TESTING"] = True
    c = app.test_client()

    def post(url, json):
        return c.post(url, json=json, headers={"X-CSRF-Token": TOKEN})

    c.post_json = post
    return c
