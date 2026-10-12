import tomllib
import unittest
from pathlib import Path

from campfire.config import DEFAULT_SERVER_URL, Settings, load_settings, write_default_config
from campfire.theme import ROLES, ThemeError, builtin_theme_names, load_theme, parse_color, parse_theme
from campfire.ui.colors import rgb_to_8, rgb_to_256

from .helpers import temp_dir


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = temp_dir()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "config.toml"

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_creates_defaults(self):
        settings, warnings = load_settings(self.path)
        self.assertEqual(settings, Settings())
        self.assertEqual(warnings, [])
        self.assertTrue(self.path.exists())
        # The generated file is valid TOML and round-trips to the same defaults.
        tomllib.loads(self.path.read_text())
        self.assertEqual(load_settings(self.path)[0], Settings())

    def test_generated_file_matches_dataclass_defaults(self):
        write_default_config(self.path)
        data = tomllib.loads(self.path.read_text())
        defaults = Settings()
        self.assertEqual(data["ui"]["theme"], defaults.theme)
        self.assertEqual(data["playback"]["seek_seconds"], defaults.seek_seconds)
        self.assertEqual(data["playback"]["previous_restart_seconds"], defaults.previous_restart_seconds)
        self.assertEqual(data["server"]["url"], DEFAULT_SERVER_URL)

    def test_values_are_loaded(self):
        self.path.write_text('[ui]\ntheme = "theme_dracula.toml"\nvisualizer = false\n'
                             '[playback]\nseek_seconds = 30\nprevious_restart_seconds = 5\n')
        settings, warnings = load_settings(self.path)
        self.assertEqual(warnings, [])
        self.assertEqual(settings.theme, "theme_dracula.toml")
        self.assertEqual(settings.visualizer, "off")  # old boolean form still accepted
        self.assertEqual(settings.seek_seconds, 30)
        self.assertEqual(settings.previous_restart_seconds, 5.0)

    def test_visualizer_and_art_choices(self):
        self.path.write_text('[ui]\nvisualizer = "meter"\nalbum_art = "blocks"\nspectrum_bands = 99\n'
                             'spectrum_style = "lasers"\n')
        settings, warnings = load_settings(self.path)
        self.assertEqual((settings.visualizer, settings.album_art), ("meter", "blocks"))
        self.assertEqual(settings.spectrum_bands, 32)          # clamped
        self.assertEqual(settings.spectrum_style, "bars")      # invalid choice -> default
        self.assertEqual(len(warnings), 2)
        self.path.write_text('[ui]\nvisualizer = true\n')
        self.assertEqual(load_settings(self.path)[0].visualizer, "spectrum")

    def test_invalid_toml_falls_back_to_defaults(self):
        self.path.write_text("[ui\ntheme = ")
        settings, warnings = load_settings(self.path)
        self.assertEqual(settings, Settings())
        self.assertIn("Invalid TOML", warnings[0])

    def test_wrong_types_and_unknown_keys_warn(self):
        self.path.write_text('[playback]\nseek_seconds = "ten"\nvolume = true\nbogus = 1\n[extra]\n')
        settings, warnings = load_settings(self.path)
        self.assertEqual(settings.seek_seconds, 10)
        self.assertEqual(settings.volume, 80)
        self.assertEqual(len(warnings), 4)

    def test_out_of_range_values_are_clamped(self):
        self.path.write_text('[playback]\nvolume = 150\nseek_seconds = 0\n'
                             '[server]\nurl = "http://example.com"\n')
        settings, warnings = load_settings(self.path)
        self.assertEqual(settings.volume, 100)
        self.assertEqual(settings.seek_seconds, 10)
        self.assertEqual(settings.server_url, DEFAULT_SERVER_URL)
        self.assertEqual(len(warnings), 3)


class ThemeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = temp_dir()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_builtin_themes_are_complete(self):
        self.assertIn("theme_default.toml", builtin_theme_names())
        for name in builtin_theme_names():
            theme, warnings = load_theme(name, self.dir)
            self.assertEqual(warnings, [], name)
            self.assertEqual(set(theme.colors), set(ROLES))

    def test_default_theme_is_black_and_orange(self):
        theme, _ = load_theme("theme_default.toml", self.dir)
        self.assertEqual(theme.colors["background"], (0, 0, 0))
        r, g, b = theme.colors["accent"]
        self.assertTrue(r == 255 and 100 < g < 170 and b < 50, theme.colors["accent"])

    def test_parse_color(self):
        self.assertEqual(parse_color("#FF8700"), (255, 135, 0))
        self.assertIsNone(parse_color("default"))
        for bad in ("orange", "#fff", "#gggggg", 12, None):
            with self.assertRaises(ThemeError):
                parse_color(bad)

    def test_user_theme_overrides_and_inherits(self):
        (self.dir / "mine.toml").write_text('name = "Mine"\n[colors]\naccent = "#00ff00"\n')
        theme, warnings = load_theme("mine", self.dir)  # extension optional
        self.assertEqual(warnings, [])
        self.assertEqual(theme.name, "Mine")
        self.assertEqual(theme.colors["accent"], (0, 255, 0))
        self.assertEqual(theme.colors["background"], (0, 0, 0))  # from the default

    def test_invalid_theme_falls_back_to_default(self):
        (self.dir / "broken.toml").write_text('[colors]\naccent = "neon"\n')
        theme, warnings = load_theme("broken.toml", self.dir)
        self.assertEqual(theme.name, "Campfire")
        self.assertIn("accent", warnings[0])

    def test_missing_and_unsafe_names_fall_back(self):
        theme, warnings = load_theme("nope.toml", self.dir)
        self.assertEqual(theme.name, "Campfire")
        self.assertIn("not found", warnings[0])
        theme, warnings = load_theme("../../etc/passwd", self.dir)
        self.assertEqual(theme.name, "Campfire")
        self.assertIn("plain file name", warnings[0])

    def test_theme_without_colors_table_is_rejected(self):
        with self.assertRaises(ThemeError):
            parse_theme('name = "x"\n', "x.toml")

    def test_unknown_color_role_warns(self):
        (self.dir / "extra.toml").write_text('[colors]\nsparkle = "#ffffff"\n')
        theme, warnings = load_theme("extra.toml", self.dir)
        self.assertIn("sparkle", warnings[0])

    def test_color_mapping(self):
        self.assertEqual(rgb_to_256((255, 135, 0)), 208)   # xterm "DarkOrange"
        self.assertEqual(rgb_to_256((0, 0, 0)), 16)
        self.assertEqual(rgb_to_256((38, 38, 38)), 235)    # nearest gray ramp entry
        self.assertEqual(rgb_to_8((250, 10, 10)), 1)        # red


if __name__ == "__main__":
    unittest.main()
