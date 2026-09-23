import json
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
