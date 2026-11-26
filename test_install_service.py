import subprocess
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from install_service import build_unit, install_unit


class ServiceTests(unittest.TestCase):
    def test_vps_unit_uses_literal_user_and_directory(self):
        unit = build_unit(PurePosixPath("/opt/issuescan"), "redctrladmin")
        self.assertIn("\nUser=redctrladmin\n", unit)
        self.assertIn("\nWorkingDirectory=/opt/issuescan\n", unit)
        self.assertIn('ExecStart="/opt/issuescan/.venv/bin/python" -u "/opt/issuescan/bot.py"', unit)

    def test_rejects_newline_injection(self):
        with self.assertRaises(ValueError):
            build_unit(PurePosixPath("/opt/issuescan"), "user\nExecStart=/other")

    def test_failed_verification_preserves_existing_unit(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "issuescan.service"
            target.write_text("existing unit", encoding="utf-8")
            with patch("install_service.subprocess.run", side_effect=subprocess.CalledProcessError(1, "verify")):
                with self.assertRaises(subprocess.CalledProcessError):
                    install_unit("replacement", target)
            self.assertEqual(target.read_text(), "existing unit")
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_replacement_is_verified_first(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "issuescan.service"
            target.write_text("existing unit", encoding="utf-8")
            def verify(args, check):
                self.assertTrue(check)
                self.assertEqual(args[:2], ["systemd-analyze", "verify"])
                self.assertEqual(Path(args[2]).read_text(), "replacement")
                self.assertEqual(target.read_text(), "existing unit")
            with patch("install_service.subprocess.run", side_effect=verify):
                install_unit("replacement", target)
            self.assertEqual(target.read_text(), "replacement")


if __name__ == "__main__":
    unittest.main()
