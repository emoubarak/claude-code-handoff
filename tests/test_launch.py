import json
import os
import tempfile
import unittest

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
        self.assertTrue(launch.restore_default_model(self.path, "opus", {"provider/model-a", "provider/model-b"}))
        self.assertEqual(self.read(), {"model": "opus", "theme": "dark"})

    def test_removes_key_when_there_was_none(self):
        self.write({"model": "provider/model-a"})
        launch.restore_default_model(self.path, None, {"provider/model-a"})
        self.assertEqual(self.read(), {})

    def test_leaves_unrelated_models_alone(self):
        self.write({"model": "sonnet"})
        self.assertFalse(launch.restore_default_model(self.path, "opus", {"provider/model-a"}))
        self.assertEqual(self.read(), {"model": "sonnet"})

    def test_missing_file(self):
        self.assertFalse(launch.restore_default_model(self.path, "opus", {"x"}))


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
