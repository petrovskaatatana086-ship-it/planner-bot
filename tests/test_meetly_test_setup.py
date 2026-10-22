"""Isolated test setup never inherits production secrets or reuses production identity."""
import json
from pathlib import Path
import shutil
import unittest
import uuid
from unittest.mock import patch

from dotenv import dotenv_values

from deploy.meetly_test_setup import SetupError, prepare


class MeetlyTestSetupTest(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd()/f".test_environment_{uuid.uuid4().hex}"
        self.root.mkdir()
        self.token = "123456789:"+"A"*35
        (self.root/".env").write_text("BOT_TOKEN=987654321:"+"B"*35+"\n", encoding="utf-8")
        self.write_local()
        self.image = "meetly-test:"+"1"*40

    def write_local(self, token=None, owner="777"):
        (self.root/".env.local").write_text(f"BOT_TOKEN={token or self.token}\nOWNER_TELEGRAM_ID={owner}\n"
            "GOOGLE_CLIENT_ID=must-not-inherit\nSMTP_HOST=must-not-inherit\n", encoding="utf-8")

    def tearDown(self):
        target = self.root.resolve()
        self.assertEqual(target.parent, Path.cwd().resolve())
        self.assertTrue(target.name.startswith(".test_environment_"))
        shutil.rmtree(target)

    def prepare(self, **overrides):
        return prepare(root=self.root, image=overrides.pop("image", self.image), **overrides)

    def test_separate_identity_fresh_data_and_secrets_are_not_in_report(self):
        report = self.prepare()
        destination = self.root/".deploy"/"meetly-test"
        values = dotenv_values(destination/"app.env", interpolate=False)
        self.assertEqual(values["BOT_TOKEN"], self.token)
        self.assertEqual(values["OWNER_TELEGRAM_ID"], "777")
        self.assertEqual(values["DATABASE_URL"], "sqlite+aiosqlite:////data/planner.db")
        self.assertEqual(values["CALENDAR_ENVIRONMENT"], "meetly-test")
        self.assertEqual(values["BOOKING_RUNTIME_ENABLED"], "true")
        self.assertEqual(values["BOT_MODE"], "polling")
        self.assertEqual(list((destination/"data").iterdir()), [])
        self.assertNotIn(self.token, json.dumps(report))
        self.assertNotIn(self.token, (destination/"compose.env").read_text())

    def test_ambient_production_and_local_google_smtp_are_not_inherited(self):
        with patch.dict("os.environ", dict(BOT_TOKEN="ambient-secret", DATABASE_URL="sqlite:///production.db",
                GOOGLE_CLIENT_ID="ambient-google", GOOGLE_CLIENT_SECRET="ambient-secret", SMTP_HOST="live-smtp",
                BOT_MODE="webhook", CALENDAR_ENVIRONMENT="production", OAUTH_ENCRYPTION_KEY="ambient-key")):
            self.prepare()
        values = dotenv_values(self.root/".deploy"/"meetly-test"/"app.env", interpolate=False)
        for key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI", "OAUTH_ENCRYPTION_KEY",
                    "SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "TELEGRAM_WEBHOOK_URL", "TELEGRAM_WEBHOOK_SECRET"):
            self.assertEqual(values[key], "", key)

    def test_same_production_bot_id_is_rejected_even_after_token_rotation(self):
        self.write_local(token="987654321:"+"C"*35)
        with self.assertRaisesRegex(SetupError, "PRODUCTION_BOT_ID_FORBIDDEN"):
            self.prepare()
        self.assertFalse((self.root/".deploy").exists())

    def test_placeholder_or_missing_test_token_cannot_generate_files(self):
        for token in ("PASTE_TEST_BOT_TOKEN_HERE", "invalid", "123:short"):
            self.write_local(token=token)
            with self.assertRaisesRegex(SetupError, "SEPARATE_TEST_TOKEN_REQUIRED"):
                self.prepare()
        (self.root/".env.local").unlink()
        with self.assertRaisesRegex(SetupError, "SEPARATE_TEST_TOKEN_REQUIRED"):
            self.prepare()
        self.assertFalse((self.root/".deploy").exists())

    def test_invalid_owner_and_conflicting_ports_are_rejected(self):
        for owner in ("0", "-1", "invalid"):
            self.write_local(owner=owner)
            with self.assertRaisesRegex(SetupError, "TEST_OWNER_REQUIRED"):
                self.prepare()
        self.write_local()
        for port in (8012, 3110, 3107, 80, 65536, True):
            with self.assertRaisesRegex(SetupError, "ISOLATED_PORT_REQUIRED"):
                self.prepare(host_port=port)
        self.assertFalse((self.root/".deploy").exists())

    def test_pinned_image_required_and_foreign_image_is_never_selected(self):
        for image in ("calendar-bot:"+"1"*40, "meetly-test:latest", "meetly-test:dev", "meetly-test:abcd"):
            with self.assertRaisesRegex(SetupError, "PINNED_TEST_IMAGE_REQUIRED"):
                self.prepare(image=image)

    def test_missing_production_identity_reference_cannot_claim_isolation(self):
        (self.root/".env").unlink()
        with self.assertRaisesRegex(SetupError, "PRODUCTION_IDENTITY_REFERENCE_REQUIRED"):
            self.prepare()
        self.assertFalse((self.root/".deploy").exists())

    def test_existing_output_is_not_modified_and_source_files_stay_unchanged(self):
        before = (self.root/".env.local").read_bytes()
        self.prepare()
        destination = self.root/".deploy"/"meetly-test"
        secret = (destination/"app.env").read_bytes()
        with self.assertRaisesRegex(SetupError, "NEW_PRIVATE_DIRECTORY_REQUIRED"):
            self.prepare()
        self.assertEqual((destination/"app.env").read_bytes(), secret)
        self.assertEqual((self.root/".env.local").read_bytes(), before)

    def test_output_cannot_escape_private_directory(self):
        for destination in (self.root/"public", self.root/".deploy", self.root/".deploy"/".."/"public"):
            with self.assertRaisesRegex(SetupError, "NEW_PRIVATE_DIRECTORY_REQUIRED"):
                self.prepare(destination=destination)
        self.assertFalse((self.root/"public").exists())
