"""Unit tests for the .env permission-hygiene check."""

import os
import stat
import tempfile
from unittest import TestCase
from unittest.mock import patch

from ai_platform.tools.env_hygiene import check_env_file_permissions


class CheckEnvFilePermissionsTests(TestCase):
    def test_returns_none_when_no_env_file_found(self):
        with patch("ai_platform.tools.env_hygiene.find_dotenv", return_value=""):
            self.assertIsNone(check_env_file_permissions())

    def test_returns_none_for_owner_only_permissions(self):
        with tempfile.NamedTemporaryFile(suffix=".env", delete=False) as f:
            path = f.name
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
            with patch("ai_platform.tools.env_hygiene.find_dotenv", return_value=path):
                self.assertIsNone(check_env_file_permissions())
        finally:
            os.remove(path)

    def test_warns_when_group_or_other_can_read(self):
        with tempfile.NamedTemporaryFile(suffix=".env", delete=False) as f:
            path = f.name
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IROTH)  # 0o604
            with patch("ai_platform.tools.env_hygiene.find_dotenv", return_value=path):
                warning = check_env_file_permissions()
            self.assertIsNotNone(warning)
            self.assertIn("chmod 600", warning)
            self.assertIn(path, warning)
        finally:
            os.remove(path)
