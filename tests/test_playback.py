import unittest

from campfire.playback.controller import PlaybackController, Status
from campfire.playback.mpv import BackendError
from campfire.playback.queue import PlayQueue

from .helpers import album, tracks


class FakeBackend:
    """Records commands; tests feed mpv-style events to the controller."""

    def __init__(self, fail_start: bool = False, visualizer: bool = False):
        self.running = False
        self.visualizer = visualizer
        self.fail_start = fail_start
        self.calls = []
        self.entry = 0

    def start(self):
        if self.fail_start:
            raise BackendError("mpv not found ('mpv').")
        self.running = True

    def load(self, url):
        self.calls.append(("load", url))

    def set_pause(self, paused):
        self.calls.append(("pause", paused))

    def seek(self, seconds, absolute=False):
        self.calls.append(("seek", seconds, absolute))

    def stop(self):
        self.calls.append(("stop",))

    def set_volume(self, volume):
        self.calls.append(("volume", volume))

    def analysis(self):
        return "analysis"

    def disable_visualizer(self):
        self.visualizer = False
        self.calls.append(("disable_visualizer",))

    def shutdown(self):
        self.running = False


ALBUM = album(1)
TRACKS = tracks(ALBUM, 4)


class QueueTests(unittest.TestCase):
    def test_navigation(self):
        q = PlayQueue()
        self.assertIsNone(q.current)
        self.assertEqual(q.set(TRACKS, 1), TRACKS[1])
        self.assertEqual(q.upcoming(), TRACKS[2:])
        self.assertEqual(q.next(), TRACKS[2])
        self.assertEqual(q.next(), TRACKS[3])
        self.assertIsNone(q.next())          # end: cursor stays on the last track
        self.assertEqual(q.current, TRACKS[3])
        self.assertEqual(q.previous(), TRACKS[2])
        self.assertEqual(q.jump(0), TRACKS[0])
        self.assertIsNone(q.previous())
        self.assertIsNone(q.jump(10))
        self.assertEqual(q.upcoming(2), TRACKS[1:3])

    def test_invalid_start_and_empty(self):
        q = PlayQueue()
        self.assertEqual(q.set(TRACKS, 99), TRACKS[0])
        self.assertIsNone(q.set([], 0))
        self.assertFalse(q.has_next())
        q.set(TRACKS)
        q.clear()
        self.assertEqual(len(q), 0)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.notices = []
        self.c = PlaybackController(self.backend, lambda t: f"url:{t.id}",
                                    lambda level, msg: self.notices.append((level, msg)),
                                    seek_seconds=10, previous_restart_seconds=3)

    def event(self, **msg):
        self.c.handle_event(msg)

    def start_playing(self, entry=1):
        self.event(event="start-file", playlist_entry_id=entry)
        self.event(event="property-change", name="idle-active", data=False)
        self.event(event="file-loaded")

    def loads(self):
        return [c[1] for c in self.backend.calls if c[0] == "load"]

    def test_play_transitions_from_loading_to_playing(self):
        self.assertEqual(self.c.status, Status.STOPPED)
        self.c.play(TRACKS, 1)
        self.assertEqual(self.loads(), [f"url:{TRACKS[1].id}"])
        self.assertEqual(self.c.status, Status.LOADING)
        self.start_playing()
        self.assertEqual(self.c.status, Status.PLAYING)
        self.assertEqual(self.c.current, TRACKS[1])

    def test_pause_state_comes_from_backend(self):
        self.c.play(TRACKS)
        self.start_playing()
        self.c.toggle_pause()
        self.assertEqual(self.backend.calls[-1], ("pause", True))
        self.assertEqual(self.c.status, Status.PLAYING)  # not until mpv confirms
        self.event(event="property-change", name="pause", data=True)
        self.assertEqual(self.c.status, Status.PAUSED)
        self.c.toggle_pause()
        self.assertEqual(self.backend.calls[-1], ("pause", False))

    def test_buffering(self):
        self.c.play(TRACKS)
        self.start_playing()
        self.event(event="property-change", name="paused-for-cache", data=True)
        self.assertEqual(self.c.status, Status.BUFFERING)

    def test_auto_advance_and_end_of_queue(self):
        self.c.play(TRACKS, 2)
        self.start_playing(1)
        self.event(event="end-file", reason="eof", playlist_entry_id=1)
        self.assertEqual(self.c.current, TRACKS[3])
        self.start_playing(2)
        self.event(event="end-file", reason="eof", playlist_entry_id=2)
        self.assertEqual(self.c.status, Status.STOPPED)
        self.assertEqual(len(self.loads()), 2)

    def test_replaced_file_end_events_are_ignored(self):
        self.c.play(TRACKS)
        self.start_playing(1)
        self.c.next()  # user skips: mpv reports the old entry ending
        self.event(event="end-file", reason="stop", playlist_entry_id=1)
        self.event(event="end-file", reason="eof", playlist_entry_id=1)  # race with natural end
        self.assertEqual(self.c.current, TRACKS[1])
        self.assertEqual(len(self.loads()), 2)

    def test_next_and_previous(self):
        self.c.play(TRACKS)
        self.start_playing()
        self.c.next()
        self.assertEqual(self.c.current, TRACKS[1])
        self.start_playing(2)
        # Early in the track: previous goes back.
        self.event(event="property-change", name="time-pos", data=1.0)
        self.c.previous()
        self.assertEqual(self.c.current, TRACKS[0])
        self.start_playing(3)
        # Later in the track: previous restarts it.
        self.event(event="property-change", name="time-pos", data=42.0)
        self.c.previous()
        self.assertEqual(self.c.current, TRACKS[0])
        self.assertEqual(self.backend.calls[-1], ("seek", 0, True))

    def test_previous_on_first_track_restarts(self):
        self.c.play(TRACKS)
        self.start_playing()
        self.c.previous()
        self.assertEqual(self.backend.calls[-1], ("seek", 0, True))

    def test_next_at_end_notifies(self):
        self.c.play(TRACKS, 3)
        self.c.next()
        self.assertEqual(self.c.current, TRACKS[3])
        self.assertIn("End of queue", self.notices[-1][1])

    def test_seek(self):
        self.c.seek(1)  # stopped: ignored
        self.assertFalse([c for c in self.backend.calls if c[0] == "seek"])
        self.c.play(TRACKS)
        self.start_playing()
        self.c.seek(-1)
        self.assertEqual(self.backend.calls[-1], ("seek", -10, False))
        self.event(event="property-change", name="seekable", data=False)
        self.c.seek(1)
        self.assertEqual(self.backend.calls[-1], ("seek", -10, False))
        self.assertIn("does not support seeking", self.notices[-1][1])

    def test_playback_error_skips_then_gives_up(self):
        self.c.play(TRACKS)
        for entry in range(1, 4):
            self.event(event="start-file", playlist_entry_id=entry)
            self.event(event="end-file", reason="error", playlist_entry_id=entry,
                       file_error="loading failed")
        self.assertEqual(self.c.status, Status.STOPPED)
        self.assertEqual(len(self.loads()), 3)
        errors = [m for level, m in self.notices if level == "error"]
        self.assertIn("loading failed", errors[0])
        self.assertIn("several tracks failed", errors[-1])

    def test_successful_load_resets_failure_count(self):
        self.c.play(TRACKS)
        self.event(event="start-file", playlist_entry_id=1)
        self.event(event="end-file", reason="error", playlist_entry_id=1, file_error="x")
        self.start_playing(2)
        self.event(event="end-file", reason="error", playlist_entry_id=2, file_error="x")
        self.event(event="start-file", playlist_entry_id=3)
        self.event(event="end-file", reason="error", playlist_entry_id=3, file_error="x")
        self.assertNotEqual(self.c.status, Status.STOPPED)

    def test_backend_exit_is_reported_and_recoverable(self):
        self.c.play(TRACKS)
        self.start_playing()
        self.backend.running = False
        self.event(event="backend-exited")
        self.assertEqual(self.c.status, Status.STOPPED)
        self.assertIn("exited unexpectedly", self.notices[-1][1])
        self.c.toggle_pause()  # restarts the current track
        self.assertTrue(self.backend.running)
        self.assertEqual(self.loads()[-1], f"url:{TRACKS[0].id}")

    def test_missing_backend_does_not_pretend_to_play(self):
        c = PlaybackController(FakeBackend(fail_start=True), lambda t: "u",
                               lambda level, msg: self.notices.append((level, msg)))
        c.play(TRACKS)
        self.assertEqual(c.status, Status.STOPPED)
        self.assertIn("mpv not found", self.notices[-1][1])

    def test_stop_and_volume(self):
        self.c.play(TRACKS)
        self.start_playing()
        self.c.stop()
        self.assertEqual(self.c.status, Status.STOPPED)
        self.c.change_volume(50)
        self.assertEqual(self.c.volume, 100)
        self.c.change_volume(-200)
        self.assertEqual(self.c.volume, 0)

    def test_duration_prefers_backend_value(self):
        self.c.play(TRACKS)
        self.assertEqual(self.c.duration, TRACKS[0].duration)
        self.event(event="property-change", name="duration", data=200.5)
        self.assertEqual(self.c.duration, 200.5)
        self.event(event="property-change", name="duration", data=None)
        self.assertEqual(self.c.duration, 200.5)

    def end(self, entry, reason="eof"):
        self.event(event="start-file", playlist_entry_id=entry)
        self.event(event="end-file", reason=reason, playlist_entry_id=entry, file_error="x")

    def test_repeat_cycles_off_all_one(self):
        self.assertEqual(self.c.repeat, "off")
        for expected in ("all", "one", "off"):
            self.c.cycle_repeat()
            self.assertEqual(self.c.repeat, expected)

    def test_repeat_one_replays_the_track(self):
        self.c.repeat = "one"
        self.c.play(TRACKS, 1)
        self.end(1)
        self.assertEqual(self.c.current, TRACKS[1])
        self.assertEqual(self.loads(), [f"url:{TRACKS[1].id}"] * 2)
        self.c.next()                                   # manual next still moves on
        self.assertEqual(self.c.current, TRACKS[2])

    def test_repeat_one_does_not_retry_a_failing_track_forever(self):
        self.c.repeat = "one"
        self.c.play(TRACKS)
        self.end(1, "error")
        self.assertEqual(self.c.current, TRACKS[1])     # skipped like without repeat

    def test_repeat_all_wraps_at_the_end(self):
        self.c.repeat = "all"
        self.c.play(TRACKS, 3)
        self.end(1)
        self.assertEqual(self.c.current, TRACKS[0])
        self.assertNotEqual(self.c.status, Status.STOPPED)
        self.c.jump(3)
        self.c.next()                                   # manual next at the end wraps too
        self.assertEqual(self.c.current, TRACKS[0])

    def test_repeat_off_stops_at_the_end(self):
        self.c.play(TRACKS, 3)
        self.end(1)
        self.assertEqual(self.c.status, Status.STOPPED)

    def shuffle_controller(self, n=12, seed=3):
        import random
        self.many = tracks(album(2), n)
        return PlaybackController(self.backend, lambda t: f"url:{t.id}",
                                  lambda level, msg: self.notices.append((level, msg)),
                                  rng=random.Random(seed))

    def test_toggling_shuffle_never_interrupts_the_current_track(self):
        c = self.shuffle_controller()
        c.play(self.many, start=3)
        loads_before = list(self.backend.calls)
        c.toggle_shuffle()
        self.assertTrue(c.shuffle)
        self.assertEqual(self.backend.calls, loads_before)        # no load, no seek, no stop
        self.assertEqual(c.current, self.many[3])
        self.assertEqual(c.queue.tracks[:4], self.many[:4])       # history and current untouched
        self.assertEqual(sorted(t.id for t in c.queue.upcoming()), sorted(t.id for t in self.many[4:]))
        self.assertNotEqual(c.queue.upcoming(), self.many[4:])
        c.toggle_shuffle()                                        # off: original order back
        self.assertFalse(c.shuffle)
        self.assertEqual(self.backend.calls, loads_before)
        self.assertEqual(c.queue.tracks, self.many)
        self.assertEqual(c.current, self.many[3])
        self.assertEqual(c.queue.upcoming(), self.many[4:])

    def test_shuffle_off_after_moving_on_keeps_playing_track(self):
        c = self.shuffle_controller()
        c.play(self.many, start=0)
        c.toggle_shuffle()
        c.next()
        now = c.current
        c.toggle_shuffle()
        self.assertIs(c.current, now)
        self.assertEqual(c.queue.tracks, self.many)
        self.assertEqual(c.queue.upcoming(), self.many[self.many.index(now) + 1:])

    def test_play_respects_shuffle_mode(self):
        c = self.shuffle_controller()
        c.toggle_shuffle()
        c.play(self.many, start=5, chosen=True)                  # a picked track plays first
        self.assertEqual(c.current, self.many[5])
        self.assertEqual(c.queue.index, 0)
        self.assertEqual(sorted(t.id for t in c.queue.tracks), sorted(t.id for t in self.many))
        self.assertNotEqual(c.queue.tracks[1:], [t for t in self.many if t is not self.many[5]])
        c.play(self.many, shuffle=False)                         # explicit in-order play
        self.assertFalse(c.shuffle)
        self.assertEqual(c.queue.tracks, self.many)

    def test_explicit_shuffle_play_turns_mode_on(self):
        c = self.shuffle_controller()
        c.play(self.many, shuffle=True)
        self.assertTrue(c.shuffle)
        self.assertEqual(c.queue.index, 0)
        self.assertNotEqual(c.queue.tracks, self.many)

    def test_repeat_all_reshuffles_each_pass(self):
        c = self.shuffle_controller()
        c.play(self.many, shuffle=True)
        first_order = list(c.queue.tracks)
        c.repeat = "all"
        c.jump(len(self.many) - 1)
        last = c.current
        c.next()
        self.assertNotEqual(c.queue.tracks, first_order)          # a new order each time around
        self.assertIsNot(c.current, last)                         # never the same track twice in a row
        self.assertEqual(len(c.queue), len(self.many))

    def test_analysis_only_while_playing(self):
        self.assertIsNone(self.c.analysis())
        self.c.play(TRACKS)
        self.start_playing()
        self.assertEqual(self.c.analysis(), "analysis")

    def test_failure_with_visualizer_disables_it_and_retries_same_track(self):
        self.backend.visualizer = True
        self.c.play(TRACKS)
        self.event(event="start-file", playlist_entry_id=1)
        self.event(event="end-file", reason="error", playlist_entry_id=1, file_error="no audio")
        self.assertIn(("disable_visualizer",), self.backend.calls)
        self.assertEqual(self.loads(), [f"url:{TRACKS[0].id}"] * 2)   # same track retried
        self.assertEqual(self.c.current, TRACKS[0])
        self.assertIn("visualizer", self.notices[-1][1])
        # A second failure is a real playback error and advances as usual.
        self.event(event="start-file", playlist_entry_id=2)
        self.event(event="end-file", reason="error", playlist_entry_id=2, file_error="no audio")
        self.assertEqual(self.c.current, TRACKS[1])


if __name__ == "__main__":
    unittest.main()
