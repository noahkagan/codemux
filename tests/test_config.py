import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from test_dashboard import app


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.entry = {"name": "agent", "host": "user@host.example", "session": "codex"}

    def write_config(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([self.entry]))

    def run_app(self, arguments):
        with patch.object(app.sys, "argv", ["codemux", *arguments]), \
             patch.object(app.sys.stdin, "isatty", return_value=True), \
             patch.object(app.sys.stdout, "isatty", return_value=True), \
             patch.object(app.curses, "wrapper", side_effect=lambda callback: callback(None)), \
             patch.object(app, "Dashboard") as dashboard:
            dashboard.return_value.run = AsyncMock()
            app.main()
            self.assertEqual(dashboard.call_args.args[1], [app.Agent(**self.entry)])
            dashboard.return_value.run.assert_awaited_once()
            return dashboard.call_args.args[2]

    def test_default_loads_user_configuration(self):
        config_home = self.root / "settings"
        self.write_config(config_home / "codemux" / "agents.json")
        with patch.dict(app.os.environ, {"XDG_CONFIG_HOME": str(config_home)}):
            self.run_app([])

    def test_empty_or_unset_xdg_uses_home_config_directory(self):
        self.write_config(self.root / ".config" / "codemux" / "agents.json")
        for value in (None, ""):
            with self.subTest(value=value), patch.dict(app.os.environ), patch.object(app.Path, "home", return_value=self.root):
                if value is None:
                    app.os.environ.pop("XDG_CONFIG_HOME", None)
                else:
                    app.os.environ["XDG_CONFIG_HOME"] = value
                self.run_app([])

    def test_explicit_config_overrides_default(self):
        explicit = self.root / "custom.json"
        self.write_config(explicit)
        with patch.dict(app.os.environ, {"XDG_CONFIG_HOME": str(self.root / "missing")}):
            self.run_app(["--config", str(explicit)])

    def test_profile_loads_from_config_directory_and_sets_note_destination(self):
        for xdg in (None, "", str(self.root / "settings")):
            with self.subTest(xdg=xdg), patch.dict(app.os.environ), \
                 patch.object(app.Path, "home", return_value=self.root):
                if xdg is None:
                    app.os.environ.pop("XDG_CONFIG_HOME", None)
                else:
                    app.os.environ["XDG_CONFIG_HOME"] = xdg
                config_home = Path(xdg) if xdg else self.root / ".config"
                path = config_home / "codemux" / "work.json"
                self.write_config(path)
                selected_path = self.run_app(["--profile", "work"])
                self.assertEqual(selected_path, path)
                app.save_note(selected_path, app.Agent(**self.entry), "Profile note")
                self.assertEqual(json.loads(path.read_text())[0]["note"], "Profile note")

    def test_invalid_profile_arguments(self):
        for arguments, message in (
            (["--profile", "work", "--config", "custom.json"], "not allowed with argument"),
            (["--profile", ""], "profile must be a nonempty name"),
            (["--profile", "../work"], "profile must be a nonempty name"),
            (["--profile", "/tmp/work"], "profile must be a nonempty name"),
        ):
            with self.subTest(arguments=arguments), \
                 patch.object(app.sys, "argv", ["codemux", *arguments]), \
                 patch.object(app.sys, "stderr", new_callable=io.StringIO) as stderr:
                with self.assertRaises(SystemExit) as error:
                    app.main()
                self.assertEqual(error.exception.code, 2)
                self.assertIn(message, stderr.getvalue())

    def test_missing_profile_does_not_fall_back_to_default(self):
        self.write_config(self.root / "codemux" / "agents.json")
        with patch.dict(app.os.environ, {"XDG_CONFIG_HOME": str(self.root)}), \
             patch.object(app.sys, "argv", ["codemux", "--profile", "missing"]), \
             patch.object(app.sys, "stderr", new_callable=io.StringIO) as stderr:
            with self.assertRaises(SystemExit) as error:
                app.main()
            self.assertEqual(error.exception.code, 2)
            self.assertIn("missing.json", stderr.getvalue())
