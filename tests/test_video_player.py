"""Video player extras on a real clip: resume, A-B loop, frame step, remembered speed, mute.

Needs ffmpeg to make the test clip; skipped without it (as on the release runner).
"""

from __future__ import annotations

import subprocess
import time

import pytest

from core.settings import get_video_position, get_video_speed
from core.video_tools import find_ffmpeg

pytestmark = pytest.mark.skipif(find_ffmpeg() is None, reason="ffmpeg isn't installed")


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    path = tmp_path_factory.mktemp("video") / "test clip ü.mp4"
    subprocess.run(
        [find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25",
         "-t", "30", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-g", "1", str(path)],
        check=True, timeout=120,
    )
    return path


def pump(qapp, condition, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


@pytest.fixture
def player(qapp):
    from ui.video_player import VideoPlayer

    widget = VideoPlayer()
    widget.resize(640, 400)
    widget.show()
    yield widget
    widget.stop()
    widget.deleteLater()


def loaded(player):
    return lambda: player.slider_seek.isEnabled() and player.player.duration() > 0


def test_resumes_where_it_was_left(player, clip, qapp):
    player.load(clip)
    assert pump(qapp, loaded(player))
    player.player.setPosition(14_000)
    assert pump(qapp, lambda: abs(player.player.position() - 14_000) < 500)
    player.stop()
    assert abs(get_video_position(clip) - 14_000) < 500

    player.load(clip)
    assert pump(qapp, lambda: loaded(player)() and player.player.position() > 13_000)
    assert "Resumed at 0:1" in player.lbl_toast.text()
    player.restart()
    assert get_video_position(clip) == 0


def test_barely_started_videos_start_over(player, clip, qapp):
    player.load(clip)
    assert pump(qapp, loaded(player))
    player.player.setPosition(3_000)
    pump(qapp, lambda: player.player.position() >= 2_500)
    player.stop()
    assert get_video_position(clip) == 0


def test_ab_loop(player, clip, qapp):
    player.load(clip, autoplay=True)
    assert pump(qapp, loaded(player))
    player.player.setPosition(5_000)
    pump(qapp, lambda: player.player.position() >= 5_000)
    player.cycle_loop()  # A
    a = player._loop[0]
    player.player.setPosition(6_000)
    pump(qapp, lambda: player.player.position() >= 6_000)
    player.cycle_loop()  # B
    b = player._loop[1]
    assert player.btn_loop.isChecked() and b > a
    assert pump(qapp, lambda: player.player.position() < b - 400, seconds=6)  # jumped back to A
    player.cycle_loop()  # off
    assert player._loop == (None, None) and not player.btn_loop.isChecked()


def test_frame_step_pauses_and_moves_one_frame(player, clip, qapp):
    from PySide6.QtMultimedia import QMediaPlayer

    player.load(clip, autoplay=True)
    assert pump(qapp, loaded(player))
    player.player.setPosition(10_000)
    pump(qapp, lambda: player.player.position() >= 10_000)
    before = player.player.position()
    player.step_frame(1)
    assert player.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState
    assert pump(qapp, lambda: 30 <= player.player.position() - before <= 60)  # 25 fps: 40 ms


def test_speed_is_remembered(player, qapp):
    from ui.video_player import VideoPlayer

    player.set_playback_rate(1.5)
    assert get_video_speed() == 1.5
    other = VideoPlayer()
    assert other.playback_rate() == 1.5
    other.deleteLater()
