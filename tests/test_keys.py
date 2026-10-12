import curses
import unittest
from pathlib import Path

from campfire.config import load_settings
from campfire.ui.keys import ACTIONS, Keymap, parse_key

from .helpers import temp_dir


class KeymapTests(unittest.TestCase):
    def test_defaults(self):
        km = Keymap()
        self.assertEqual(km.warnings, [])
        self.assertEqual(km.action(" "), "play_pause")
        self.assertEqual(km.action(curses.KEY_LEFT), "seek_back")
        self.assertEqual([km.action(k) for k in "1234"], ["artists", "albums", "tracks", "queue"])
        self.assertEqual(km.action("="), "volume_up")
        self.assertEqual(km.label("artists"), "1")
        self.assertEqual(km.label("play_pause"), "space")
        self.assertEqual(km.label("seek_forward"), "→")
        # Every action has a key by default and no key is shared.
        self.assertTrue(all(km.names[a] for a in ACTIONS))

    def test_parse_key(self):
        self.assertEqual(parse_key("G"), "G")
        self.assertEqual(parse_key("Space"), " ")
        self.assertEqual(parse_key("f5"), curses.KEY_F5)
        for bad in ("", "enter", "up", "Esc", "ctrl-x", "\x01"):
            with self.assertRaises(ValueError):
                parse_key(bad)

    def test_user_bindings_replace_defaults(self):
        km = Keymap({"artists": "a", "albums": ["b", "f2"], "next": "N"})
        self.assertEqual(km.warnings, [])
        self.assertEqual(km.action("a"), "artists")
        self.assertIsNone(km.action("1"))           # old default no longer bound
        self.assertEqual(km.action(curses.KEY_F2), "albums")
        self.assertEqual(km.label("albums"), "b")
        self.assertEqual(km.action("N"), "next")
        self.assertIsNone(km.action("n"))

    def test_user_binding_can_take_a_default_key(self):
        km = Keymap({"search": "n"})
        self.assertEqual(km.action("n"), "search")
        self.assertEqual(km.names["next"], [])
        self.assertIn("next", km.warnings[0])

    def test_invalid_bindings_warn_and_fall_back(self):
        km = Keymap({"bogus": "x", "quit": "enter", "stop": 5, "help": ["?", "?"]})
        self.assertEqual(km.action("q"), "quit")          # fell back to the default
        self.assertEqual(km.action("s"), "stop")
        self.assertEqual(km.names["help"], ["?"])
        text = " ".join(km.warnings)
        for fragment in ("bogus", "reserved", "keys.stop"):
            self.assertIn(fragment, text)

    def test_conflicting_user_bindings(self):
        km = Keymap({"next": "x", "previous": "x"})
        self.assertEqual(km.action("x"), "next")
        self.assertEqual(km.action("p"), "previous")      # previous kept its default
        self.assertTrue(any("already bound" in w for w in km.warnings))

    def test_keys_table_from_config(self):
        with temp_dir() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('[keys]\nartists = "a"\nalbums = ["l", "f2"]\n')
            settings, warnings = load_settings(path)
            self.assertEqual(warnings, [])
            self.assertEqual(settings.keys, {"artists": "a", "albums": ["l", "f2"]})
            path.write_text('keys = 5\n')
            settings, warnings = load_settings(path)
            self.assertEqual(settings.keys, {})
            self.assertIn("[keys]", warnings[0])


if __name__ == "__main__":
    unittest.main()
