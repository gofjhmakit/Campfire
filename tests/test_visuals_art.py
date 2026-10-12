import math
import shutil
import subprocess
import unittest
from pathlib import Path

from campfire import art
from campfire.playback.mpv import analysis_filter, band_frequencies, parse_analysis
from campfire.ui.graphics import DIACRITICS, PLACEHOLDER, KittyImages
from campfire.ui.visuals import Dynamics, bar_layout, block_rows, draw_spectrum, gradient, normalize

from .helpers import temp_dir

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


class AnalysisTests(unittest.TestCase):
    def test_band_frequencies_are_log_spaced(self):
        f = band_frequencies(16)
        self.assertEqual((len(f), f[0], f[-1]), (16, 40, 16000))
        ratios = [b / a for a, b in zip(f, f[1:])]
        self.assertLess(max(ratios) - min(ratios), 0.05)

    def test_parse_analysis(self):
        data = {f"lavfi.astats.{i}.RMS_level": str(-10.0 * i) for i in range(1, 7)}
        data["lavfi.astats.2.RMS_level"] = "-inf"
        a = parse_analysis(data, 4)
        self.assertEqual(a.bands[0], -10.0)
        self.assertTrue(math.isinf(a.bands[1]))
        self.assertEqual(a.levels, [-50.0, -60.0])
        self.assertIsNone(parse_analysis({"lavfi.astats.1.RMS_level": "-3"}, 4))  # incomplete
        self.assertIsNone(parse_analysis(None, 4))

    @unittest.skipUnless(HAVE_FFMPEG, "needs the ffmpeg command-line tool")
    def test_filter_measures_the_right_bands_and_leaves_audio_unchanged(self):
        # Integration test of the real filter graph (mpv uses the same libavfilter).
        bands = 16
        graph = analysis_filter(bands).split(":", 1)[1].removeprefix("lavfi=[").removesuffix("]")
        out = subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=100:d=1",
             "-f", "lavfi", "-i", "sine=f=3000:d=1", "-filter_complex",
             f"[0][1]amix=inputs=2,aformat=channel_layouts=stereo,{graph},ametadata=print:file=-",
             "-f", "null", "-"], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        last = {}
        for line in out.stdout.splitlines():
            if "RMS_level" in line:
                key, value = line.split("=")
                last[int(key.split(".")[2])] = float(value)
        levels = [last[i] for i in range(1, bands + 1)]
        freqs = band_frequencies(bands)
        loudest = sorted(range(bands), key=lambda i: levels[i])[-2:]
        self.assertEqual(sorted(freqs[i] for i in loudest), [89, 3238])
        self.assertIn(bands + 1, last)  # stereo levels follow the bands


class DynamicsTests(unittest.TestCase):
    def test_instant_rise_smooth_fall_and_peak_hold(self):
        d = Dynamics()
        d.update([1.0, 0.5], 0.05)
        self.assertEqual(d.bars, [1.0, 0.5])
        d.update([0.0, 0.0], 0.1)
        self.assertAlmostEqual(d.bars[0], 1.0 - 0.18)
        self.assertEqual(d.peaks[0], 1.0)            # held
        for _ in range(20):
            d.update([0.0, 0.0], 0.1)
        self.assertEqual(d.bars, [0.0, 0.0])
        self.assertLess(d.peaks[0], 1.0)
        self.assertFalse(d.update(None, 0.5) and any(d.peaks))  # eventually silent

    def test_normalize(self):
        self.assertEqual(normalize(-100), 0.0)
        self.assertEqual(normalize(0), 1.0)
        self.assertEqual(normalize(float("nan")), 0.0)
        self.assertEqual(normalize(float("-inf")), 0.0)

    def test_gradient_and_layout(self):
        self.assertEqual(gradient((0, 0, 0), (100, 100, 100), (200, 200, 200), 0.0), (0, 0, 0))
        self.assertEqual(gradient((0, 0, 0), (100, 100, 100), (200, 200, 200), 0.5), (100, 100, 100))
        self.assertEqual(bar_layout(31, 16), (1, 1))
        self.assertEqual(bar_layout(100, 16), (4, 1))
        self.assertEqual(bar_layout(16, 16), (1, 0))
        self.assertEqual(bar_layout(10, 16), (0, 0))

    def test_draw_spectrum_stays_in_its_box(self):
        d = Dynamics()
        d.update([1.0, 0.3, 0.0, 0.7], 0.05)
        cells = []
        colors = dict.fromkeys(("spectrum_low", "spectrum_mid", "spectrum_high", "spectrum_peak"), (1, 2, 3))
        draw_spectrum(lambda y, x, s, a: cells.append((y, x, s)), lambda c: 0, colors, d, 10, 5, 8, 4)
        for y, x, s in cells:
            self.assertTrue(10 <= y < 14 and 5 <= x and x + len(s) <= 13, (y, x, s))
        self.assertEqual(sum(1 for y, x, s in cells if x == cells[0][1]), 4)  # full bar: 4 rows


class ArtTests(unittest.TestCase):
    def test_unknown_formats_never_reach_ffmpeg(self):
        for data in (b"#EXTM3U\nfile:///etc/passwd", b"<svg/>", b"", b"RIFF1234WAVE"):
            with self.assertRaises(art.ArtError):
                art.decode("x", data)

    @unittest.skipUnless(HAVE_FFMPEG, "needs the ffmpeg command-line tool")
    def test_decode_and_downscale(self):
        jpeg = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                               "color=c=red:s=300x200", "-frames:v", "1", "-f", "image2pipe",
                               "-c:v", "mjpeg", "pipe:1"], capture_output=True).stdout
        artwork = art.decode("c", jpeg)
        self.assertTrue(artwork.png.startswith(b"\x89PNG"))
        grid = artwork.downscale(4, 6)
        self.assertEqual((len(grid), len(grid[0])), (6, 4))
        r, g, b = grid[3][2]
        self.assertTrue(r > 200 and g < 60 and b < 60, grid[3][2])
        self.assertEqual(len(block_rows(grid)), 3)
        with self.assertRaises(art.ArtError):
            art.decode("c", b"\xff\xd8\xff" + b"garbage" * 10)

    def test_cache_uses_hashed_private_files(self):
        with temp_dir() as tmp:
            cache = art.ArtCache(Path(tmp) / "art")
            cache.put("../../evil", b"data")
            self.assertEqual(cache.get("../../evil"), b"data")
            files = list((Path(tmp) / "art").iterdir())
            self.assertEqual(len(files), 1)
            self.assertNotIn("evil", files[0].name)
            self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
            cache.discard("../../evil")
            self.assertIsNone(cache.get("../../evil"))


class KittyTests(unittest.TestCase):
    def test_upload_once_and_virtual_placement(self):
        out = []
        k = KittyImages(write=out.append)
        png = b"\x89PNG" + b"x" * 5000
        image_id = k.image("cover", png, 1, 6, 3)
        sent = b"".join(out).decode()
        self.assertEqual(image_id, 16)
        self.assertIn("a=t,f=100,t=d,i=16,q=2,m=1", sent)         # chunked upload
        self.assertIn("a=p,U=1,i=16,c=6,r=3,q=2", sent)          # virtual placement
        out.clear()
        self.assertEqual(k.image("cover", png, 1, 6, 3), 16)
        self.assertEqual(out, [])                                 # nothing new to send
        k.image("cover", png, 1, 8, 4)                            # resized: re-placed, not re-uploaded
        sent = b"".join(out).decode()
        self.assertIn("a=p,U=1,i=16,c=8,r=4", sent)
        self.assertNotIn("a=t", sent)
        self.assertEqual(k.image("cover", png, 2, 30, 15), 17)    # other slot: own image id
        self.assertTrue(all(b"q=2" in c for c in out))            # terminal never replies

    def test_tmux_passthrough_wrapping(self):
        out = []
        KittyImages(tmux=True, write=out.append).image("c", b"png", 1, 2, 2)
        for chunk in out:
            self.assertTrue(chunk.startswith(b"\x1bPtmux;\x1b\x1b_G"))
            self.assertTrue(chunk.endswith(b"\x1b\x1b\\\x1b\\"))

    def test_placeholders_encode_row_and_column(self):
        lines = KittyImages(write=lambda d: None).placeholder_lines(3, 2)
        self.assertEqual(len(lines), 2)
        cells = [lines[1][i:i + 3] for i in range(0, len(lines[1]), 3)]
        self.assertEqual(len(cells), 3)
        self.assertTrue(all(c[0] == PLACEHOLDER and c[1] == DIACRITICS[1] for c in cells))
        self.assertEqual([c[2] for c in cells], list(DIACRITICS[:3]))
        self.assertEqual(DIACRITICS[:3], "\u0305\u030d\u030e")  # kitty's table

    def test_ids_are_recycled_least_recently_used(self):
        out = []
        k = KittyImages(write=out.append)
        for n in range(240):
            k.image(f"c{n}", b"p", 1, 2, 2)
        k.image("c0", b"p", 1, 2, 2)              # touch the oldest
        out.clear()
        self.assertEqual(k.image("new", b"p", 1, 2, 2), 17)  # evicts c1 (id 17), not c0
        self.assertIn(b"a=d,d=I,i=17", b"".join(out))


if __name__ == "__main__":
    unittest.main()
