import smtplib
import unittest
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import patch

from app.services.email import _deliver_email


class RecordingSMTP:
    send_message = smtplib.SMTP.send_message

    def __init__(self):
        self.envelopes = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def ehlo(self):
        pass

    def ehlo_or_helo_if_needed(self):
        pass

    def login(self, *args):
        pass

    def sendmail(self, sender, recipients, data, mail_options=(), rcpt_options=()):
        self.envelopes.append((recipients, data))
        return {}


class AdminEmailCopyTest(unittest.TestCase):
    def deliver(self, admin, recipient="client@example.com", bcc=None):
        settings = SimpleNamespace(smtp_admin_copy_email=admin, smtp_use_ssl=True,
            smtp_host="example.com", smtp_port=465, smtp_use_tls=False,
            smtp_username="sender", smtp_password="fake")
        message = EmailMessage()
        message["From"] = "sender@example.com"
        message["To"] = recipient
        if bcc:
            message["Bcc"] = bcc
        message.set_content("Booking notice")
        smtp = RecordingSMTP()
        with patch("app.services.email.smtplib.SMTP_SSL", return_value=smtp):
            _deliver_email(settings, message)
        return smtp.envelopes[0]

    def test_copy_is_in_envelope_but_not_visible_to_client(self):
        recipients, data = self.deliver("admin@example.com")
        self.assertEqual(recipients, ["client@example.com", "admin@example.com"])
        self.assertNotIn(b"admin@example.com", data)
        self.assertNotIn(b"Bcc:", data)

    def test_same_recipient_does_not_get_duplicate_copy(self):
        recipients, _ = self.deliver("CLIENT@example.com")
        self.assertEqual(recipients, ["client@example.com"])

    def test_disabled_copy_preserves_client_only_delivery(self):
        recipients, _ = self.deliver("")
        self.assertEqual(recipients, ["client@example.com"])

    def test_existing_hidden_recipients_are_preserved(self):
        recipients, data = self.deliver("admin@example.com", bcc="archive@example.com")
        self.assertEqual(recipients, ["client@example.com", "archive@example.com", "admin@example.com"])
        self.assertNotIn(b"Bcc:", data)
