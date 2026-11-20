import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from speaker_preferences import MAX_RATIO, PreferencesError, SpeakerPreferences


class SpeakerPreferencesTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "speaker_profiles.json"

    def write(self, value):
        self.path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def test_missing_file_and_unknown_devices_have_compatible_defaults(self):
        preferences = SpeakerPreferences(self.path)
        self.assertEqual(preferences.profiles, {})
        self.assertEqual(preferences.ratio("stable-a"), 1.0)
        self.assertTrue(preferences.selected("stable-a"))
        self.assertFalse(self.path.exists())
        self.assertEqual(preferences.profiles, {})

    def test_ratio_selection_and_calibration_survive_restart(self):
        original = {
            "stable-a": {"name": "客厅", "max_db": 0, "custom": {"position": "left"}},
            "not-currently-visible": {"name": "书房", "max_db": -12, "ratio": .8,
                                      "selected": False, "future_field": [1, 2]},
            "_future_metadata": "preserve without coercion",
        }
        self.write(original)
        preferences = SpeakerPreferences(self.path)
        before = self.path.read_bytes()
        preferences.set_ratio("stable-a", 2.5)
        preferences.set_selected("stable-a", False)
        self.assertEqual(self.path.read_bytes(), before, "Setters must not write to disk.")
        preferences.save()
        loaded = SpeakerPreferences(self.path)
        self.assertEqual(loaded.ratio("stable-a"), 2.5)
        self.assertFalse(loaded.selected("stable-a"))
        self.assertEqual(loaded.profiles["stable-a"]["name"], "客厅")
        self.assertEqual(loaded.profiles["stable-a"]["max_db"], 0)
        self.assertEqual(loaded.profiles["stable-a"]["custom"], {"position": "left"})
        self.assertEqual(loaded.profiles["not-currently-visible"], original["not-currently-visible"])
        self.assertEqual(loaded.profiles["_future_metadata"], "preserve without coercion")

    def test_old_name_and_max_db_only_records_keep_defaults(self):
        self.write({"stable-a": {"name": "客厅", "max_db": -12}})
        preferences = SpeakerPreferences(self.path)
        self.assertEqual(preferences.ratio("stable-a"), 1.0)
        self.assertTrue(preferences.selected("stable-a"))
        preferences.save()
        self.assertEqual(SpeakerPreferences(self.path).profiles,
                         {"stable-a": {"name": "客厅", "max_db": -12}})

    def test_ratio_rejects_bool_nonfinite_negative_and_above_limit(self):
        preferences = SpeakerPreferences(self.path)
        for value in (True, False, None, "2", float("nan"), float("inf"),
                      -float("inf"), -.01, MAX_RATIO + .01, 10 ** 1000):
            with self.subTest(value=value), self.assertRaises(ValueError):
                preferences.set_ratio("stable-a", value)
        self.assertEqual(preferences.profiles, {})
        for value in (0, .05, 1, 100.000002, MAX_RATIO):
            preferences.set_ratio("stable-a", value)
            self.assertEqual(preferences.ratio("stable-a"), value)

    def test_selected_accepts_only_booleans(self):
        preferences = SpeakerPreferences(self.path)
        for value in (0, 1, "true", None, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                preferences.set_selected("stable-a", value)
        self.assertEqual(preferences.profiles, {})

    def test_corrupt_input_is_an_explicit_error_and_is_never_modified(self):
        for raw in (b'{"broken":', b"[]", b"", b"\xff",
                    b'{"x":{"max_db":0},"x":{"name":"duplicate"}}',
                    b'{"x":{"ratio":NaN}}', b'{"x":{"ratio":true}}',
                    ('{"x":{"ratio":' + '9' * 400 + '}}').encode(),
                    b'{"x":{"ratio":-1}}', b'{"x":{"selected":1}}'):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                with self.assertRaises(PreferencesError):
                    SpeakerPreferences(self.path)
                self.assertEqual(self.path.read_bytes(), raw)

    def test_bom_input_is_read_without_discarding_calibration(self):
        self.path.write_text('{"stable-a":{"max_db":-8,"name":"客厅"}}', encoding="utf-8-sig")
        preferences = SpeakerPreferences(self.path)
        preferences.set_selected("stable-a", False)
        preferences.save()
        self.assertEqual(SpeakerPreferences(self.path).profiles["stable-a"]["max_db"], -8)

    def test_failed_atomic_replace_leaves_original_and_surfaces_error(self):
        self.write({"stable-a": {"max_db": 0, "name": "客厅"}})
        original = self.path.read_bytes()
        preferences = SpeakerPreferences(self.path)
        preferences.set_ratio("stable-a", 1.7)
        with patch("speaker_preferences.os.replace", side_effect=PermissionError("denied")):
            with self.assertRaises(PreferencesError):
                preferences.save()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        preferences.save()
        self.assertEqual(SpeakerPreferences(self.path).ratio("stable-a"), 1.7)

    def test_external_file_change_is_not_overwritten(self):
        self.write({"stable-a": {"max_db": 0}})
        preferences = SpeakerPreferences(self.path)
        preferences.set_ratio("stable-a", 1.2)
        external = b'{"stable-a":{"max_db":-8},"unknown":{"name":"new-record"}}'
        self.path.write_bytes(external)
        with self.assertRaises(PreferencesError):
            preferences.save()
        self.assertEqual(self.path.read_bytes(), external)

    def test_direct_profile_mutation_is_validated_before_save(self):
        self.write({"stable-a": {"max_db": 0}})
        original = self.path.read_bytes()
        preferences = SpeakerPreferences(self.path)
        preferences.profiles["stable-a"]["ratio"] = float("inf")
        with self.assertRaises(PreferencesError):
            preferences.save()
        self.assertEqual(self.path.read_bytes(), original)

    def test_new_records_and_nested_parent_are_saved_atomically(self):
        path = self.path.parent / "nested" / "speaker_profiles.json"
        preferences = SpeakerPreferences(path)
        preferences.set_ratio("stable-a", 0)
        preferences.set_selected("stable-b", False)
        preferences.save()
        loaded = SpeakerPreferences(path)
        self.assertEqual(loaded.ratio("stable-a"), 0)
        self.assertFalse(loaded.selected("stable-b"))
        self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_unknown_nonobject_record_cannot_be_silently_replaced(self):
        self.write({"stable-a": "unknown-format"})
        preferences = SpeakerPreferences(self.path)
        with self.assertRaises(PreferencesError):
            preferences.set_ratio("stable-a", 2)
        self.assertEqual(preferences.profiles["stable-a"], "unknown-format")


if __name__ == "__main__":
    unittest.main()
