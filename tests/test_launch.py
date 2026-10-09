import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from claude_handoff import cli, launch


class RestoreDefaultModelTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "settings.json")

    def tearDown(self):
        self.dir.cleanup()

    def write(self, data):
        with open(self.path, "w") as file:
            json.dump(data, file)

    def read(self):
        with open(self.path) as file:
            return json.load(file)

    def test_restores_previous_model(self):
        self.write({"model": "provider/model-b", "theme": "dark"})
        self.assertEqual(launch.restore_default_model(self.path, "opus", {"provider/model-a", "provider/model-b"}), "restored")
        self.assertEqual(self.read(), {"model": "opus", "theme": "dark"})

    def test_removes_key_when_there_was_none(self):
        self.write({"model": "provider/model-a"})
        launch.restore_default_model(self.path, None, {"provider/model-a"})
        self.assertEqual(self.read(), {})

    def test_leaves_unrelated_models_alone(self):
        self.write({"model": "sonnet"})
        self.assertEqual(launch.restore_default_model(self.path, "opus", {"provider/model-a"}), "unchanged")
        self.assertEqual(self.read(), {"model": "sonnet"})

    def test_missing_file(self):
        self.assertEqual(launch.restore_default_model(self.path, "opus", {"x"}), "unchanged")

    def test_symlink_permissions_and_no_leftovers(self):
        real_dir = os.path.join(self.dir.name, "dotfiles")
        os.makedirs(real_dir)
        real = os.path.join(real_dir, "settings.json")
        with open(real, "w") as file:
            json.dump({"model": "provider/model-a"}, file)
        os.chmod(real, 0o600)
        os.symlink(real, self.path)
        self.assertEqual(launch.restore_default_model(self.path, "opus", {"provider/model-a"}), "restored")
        self.assertTrue(os.path.islink(self.path))
        self.assertEqual(stat.S_IMODE(os.stat(real).st_mode), 0o600)
        self.assertEqual(self.read(), {"model": "opus"})
        self.assertEqual(sorted(os.listdir(real_dir)), ["settings.json"])
        self.assertEqual(sorted(os.listdir(self.dir.name)), ["dotfiles", "settings.json"])

    def test_write_failure_does_not_raise(self):
        self.write({"model": "provider/model-a"})
        os.chmod(self.dir.name, 0o500)  # cannot create the temporary file
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(launch.restore_default_model(self.path, "opus", {"provider/model-a"}), "failed")
        finally:
            os.chmod(self.dir.name, 0o700)
        self.assertIn("could not restore", stderr.getvalue())
        self.assertEqual(self.read(), {"model": "provider/model-a"})


class DefaultModelGuardTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.settings = os.path.join(self.dir.name, "settings.json")
        self.state = os.path.join(self.dir.name, "state")
        os.makedirs(self.state)
        self.set_model("opus")

    def set_model(self, model):
        with open(self.settings, "w") as file:
            json.dump({"model": model}, file)

    def model(self):
        return launch.read_default_model(self.settings)

    def guard(self):
        return launch.DefaultModelGuard(self.settings, self.state)

    def test_exit_restores(self):
        guard = self.guard()
        guard.enter(["p/a", "p/b"])
        self.set_model("p/b")  # /model during the session
        guard.exit()
        self.assertEqual(self.model(), "opus")
        self.assertFalse(os.path.exists(guard.record))

    def dead_pid(self):
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        process.wait()
        return process.pid

    def test_killed_launcher_is_repaired_by_next_launch(self):
        guard = self.guard()
        guard.enter(["p/a"])
        self.set_model("p/a")
        # simulate SIGKILL: the record stays, with a pid that no longer runs
        with open(guard.record) as file:
            data = json.load(file)
        data["pids"] = [self.dead_pid()]
        with open(guard.record, "w") as file:
            json.dump(data, file)
        # the next launch must not take "p/a" as the user's default
        guard.enter(["local-model"])
        self.assertEqual(self.model(), "opus")
        with open(guard.record) as file:
            self.assertEqual(json.load(file)["before"], "opus")
        guard.exit()
        self.assertEqual(self.model(), "opus")

    def test_killed_launcher_is_repaired_by_anthropic(self):
        guard = self.guard()
        guard.enter(["p/a"])
        self.set_model("p/a")
        with mock.patch.object(launch, "_alive", return_value=False):
            guard.recover()
        self.assertEqual(self.model(), "opus")
        self.assertFalse(os.path.exists(guard.record))

    def test_parallel_sessions_keep_the_original_default(self):
        first = self.guard()
        first.enter(["p/a"])
        self.set_model("p/a")  # first session changed the default
        second = self.guard()
        with open(first.record) as file:
            data = json.load(file)
        data["pids"].append(os.getppid())  # another live launcher
        with open(first.record, "w") as file:
            json.dump(data, file)
        second.enter(["local-model"])
        with open(first.record) as file:
            record = json.load(file)
        self.assertEqual(record["before"], "opus")
        self.assertEqual(record["models"], ["local-model", "p/a"])
        first.exit()  # restores, the other launcher is still recorded
        self.assertEqual(self.model(), "opus")
        self.assertTrue(os.path.exists(first.record))

    def test_record_is_kept_until_the_restore_succeeds(self):
        guard = self.guard()
        guard.enter(["p/a"])
        self.set_model("p/a")
        os.chmod(self.dir.name, 0o500)  # settings.json cannot be replaced
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                guard.exit()
            self.assertEqual(self.model(), "p/a")
            self.assertTrue(os.path.exists(guard.record))
            with open(guard.record) as file:
                self.assertEqual(json.load(file)["before"], "opus")
            with contextlib.redirect_stderr(io.StringIO()), mock.patch.object(launch, "_alive", return_value=False):
                guard.recover()  # still read-only: the record must survive this too
            self.assertTrue(os.path.exists(guard.record))
        finally:
            os.chmod(self.dir.name, 0o700)
        with mock.patch.object(launch, "_alive", return_value=False):
            guard.recover()
        self.assertEqual(self.model(), "opus")
        self.assertFalse(os.path.exists(guard.record))

    def test_user_choice_of_an_anthropic_model_is_kept(self):
        guard = self.guard()
        guard.enter(["p/a"])
        self.set_model("sonnet")
        guard.exit()
        self.assertEqual(self.model(), "sonnet")


class EnvironmentTest(unittest.TestCase):
    def test_upstream_credentials_do_not_reach_claude(self):
        environ = {"PATH": "/bin", "OPENROUTER_API_KEY": "fake-openrouter-key-for-tests", "CLAUDE_HANDOFF_LOCAL_API_KEY": "local-secret",
                   "ANTHROPIC_API_KEY": "sk-ant", "MY_COPY": "fake-openrouter-key-for-tests", "CLAUDE_HANDOFF_PROXY_TOKEN": "t",
                   "UNRELATED": "keep"}
        with mock.patch.dict(os.environ, environ, clear=True):
            env = launch.claude_environment("http://127.0.0.1:1", "tok", "m", secrets_to_drop=("fake-openrouter-key-for-tests",))
        for name in ("OPENROUTER_API_KEY", "CLAUDE_HANDOFF_LOCAL_API_KEY", "ANTHROPIC_API_KEY", "MY_COPY",
                     "CLAUDE_HANDOFF_PROXY_TOKEN"):
            self.assertNotIn(name, env)
        self.assertNotIn("fake-openrouter-key-for-tests", json.dumps(env))
        self.assertEqual(env["UNRELATED"], "keep")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "tok")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:1")

    def test_oauth_token_is_dropped(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-something-long"}, clear=True):
            env = launch.claude_environment("http://127.0.0.1:1", "tok", "m")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)

    def test_short_key_values_do_not_remove_unrelated_variables(self):
        environ = {"DISABLE_TELEMETRY": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                   "DISABLE_AUTOUPDATER": "1", "SHLVL": "1", "CLAUDE_HANDOFF_LOCAL_API_KEY": "1"}
        with mock.patch.dict(os.environ, environ, clear=True):
            env = launch.claude_environment("http://127.0.0.1:1", "tok", "m", secrets_to_drop=("1",))
        for name in ("DISABLE_TELEMETRY", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "DISABLE_AUTOUPDATER", "SHLVL"):
            self.assertEqual(env[name], "1")
        self.assertNotIn("CLAUDE_HANDOFF_LOCAL_API_KEY", env)  # still dropped by name


class ParsingTest(unittest.TestCase):
    def test_parse_models(self):
        self.assertEqual(launch.parse_models("a/b=Nice name, c/d ,"), [("a/b", "Nice name"), ("c/d", "c/d")])
        self.assertEqual(launch.parse_models(None), [])

    def test_parse_routes(self):
        self.assertEqual(launch.parse_routes("a/b=prov1,c/d=prov2"), {"a/b": "prov1", "c/d": "prov2"})

    def test_picker_settings(self):
        data = json.loads(launch.picker_settings([("a/b", "A B")]))
        self.assertEqual(data, {"modelPicker": {"replaceBuiltInOptions": True,
                                                "options": [{"model": "a/b", "label": "A B"}]}})

    def test_split_options_stops_at_first_claude_argument(self):
        options, rest = cli.split_options(
            ["-m", "x/y", "--route=x/y=p", "--resume", "abc", "-m", "not-ours"],
            {"-m": "model", "--route": "route"},
        )
        self.assertEqual(options, {"model": "x/y", "route": "x/y=p"})
        self.assertEqual(rest, ["--resume", "abc", "-m", "not-ours"])

    def test_split_options_double_dash(self):
        options, rest = cli.split_options(["--", "-m", "x"], {"-m": "model"})
        self.assertEqual(options, {})
        self.assertEqual(rest, ["-m", "x"])

    def test_merge_models_puts_launch_model_first(self):
        self.assertEqual(cli.merge_models("b", [("a", "A"), ("b", "B")]), [("b", "B"), ("a", "A")])


if __name__ == "__main__":
    unittest.main()
