"""HDRIs as lighting (core.environment): the sums, and which images can be picked."""

from __future__ import annotations

import math
import zipfile

import numpy as np
import pytest

from core import archives, environment, hdr_image
from tests.test_hdr_image import write_exr, write_hdr


def sky(height=128, value=0.5):
    return np.full((height, height * 2, 3), value, dtype=np.float32)


def with_sun(height=128, direction=(0.0, 0.6, -0.8), strength=20000.0, radius=0.03):
    """A dim room with one small, very bright disc in it: most of the light is in a few pixels."""
    d, _solid = environment.directions(height, height * 2)
    pixels = np.full((height, height * 2, 3), 0.2, dtype=np.float64)
    pixels[d @ (np.array(direction) / np.linalg.norm(direction)) > 1 - radius * radius] = strength
    return pixels


def light_in(pixels):
    d, solid = environment.directions(*pixels.shape[:2])
    return (pixels @ np.array([0.2126, 0.7152, 0.0722]) * solid).sum()


# ----------------------------------------------------------------------
# Shrinking
# ----------------------------------------------------------------------
@pytest.mark.parametrize("size", [(1024, 2048), (1000, 2000), (768, 1536)])
def test_shrinking_keeps_the_light(size):
    pixels = np.ones((*size, 3), dtype=np.float32)
    pixels[size[0] // 3, size[1] // 3] = 5000.0  # (a sun in one pixel)
    small = environment.area_average(pixels, 256)
    assert small.shape == (128, 256, 3)
    assert small.mean() == pytest.approx(pixels.mean(), rel=0.01)


def test_shrinking_is_more_truthful_than_skipping_pixels():
    """Skipping pixels, as thumbnails may, can miss the sun: then nearly all the light is gone."""
    pixels = np.full((1024, 2048, 3), 0.1, dtype=np.float32)
    pixels[301, 901] = 400000.0
    assert environment.area_average(pixels, 256).mean() == pytest.approx(pixels.mean(), rel=0.01)
    assert pixels[::8, ::8].mean() < pixels.mean() * 0.5


def test_small_panoramas_are_not_made_bigger():
    pixels = sky(64)
    assert environment.area_average(pixels, 256).shape == (64, 128, 3)


# ----------------------------------------------------------------------
# Directions
# ----------------------------------------------------------------------
def test_a_panoramas_directions():
    d, solid = environment.directions(64, 128)
    assert np.linalg.norm(d, axis=-1) == pytest.approx(1.0)
    assert d[0, 64][1] > 0.99 and d[-1, 64][1] < -0.99  # (the top row is straight up)
    assert d[32, 64] @ np.array([0, 0, -1]) > 0.99  # (the middle of the image is straight ahead)
    assert d[32, 96][0] > 0.99  # (a quarter of the way on, to the right: seen from inside, nothing is mirrored)
    assert solid.sum() == pytest.approx(4 * math.pi, rel=0.001)


# ----------------------------------------------------------------------
# Soft light
# ----------------------------------------------------------------------
def test_an_even_sky_lights_every_side_alike():
    harmonics = environment.project_harmonics(sky())
    d, _solid = environment.directions(16, 32)
    light = environment.irradiance(harmonics, d.reshape(-1, 3))
    assert light == pytest.approx(math.pi * 0.5, rel=0.002)  # (a white surface under a sky of 0.5 shows 0.5)


def test_harmonics_follow_a_cosine():
    """Light from straight above: surfaces get it as the cosine of their angle to it."""
    d, _solid = environment.directions(128, 256)
    pixels = np.where(d[..., 1:2] > 0.0, 1.0, 0.0) * np.ones((128, 256, 3))  # (a white sky, a black ground)
    harmonics = environment.project_harmonics(pixels)
    up = environment.irradiance(harmonics, np.array([[0.0, 1.0, 0.0]]))[0, 0]
    side = environment.irradiance(harmonics, np.array([[1.0, 0.0, 0.0]]))[0, 0]
    down = environment.irradiance(harmonics, np.array([[0.0, -1.0, 0.0]]))[0, 0]
    assert up == pytest.approx(math.pi, rel=0.03)  # (the whole sky's cosine-weighted light)
    assert side == pytest.approx(math.pi / 2, rel=0.03)  # (half of it)
    assert abs(down) < 0.1


# ----------------------------------------------------------------------
# Lights
# ----------------------------------------------------------------------
def test_a_sun_becomes_a_light():
    pixels = with_sun()
    lights, rest = environment.extract_lights(pixels)
    assert len(lights) == 1
    assert np.dot(lights[0].direction, np.array([0.0, 0.6, -0.8])) > 0.999
    assert max(lights[0].color) > 1.0
    assert rest.max() < pixels.max() / 10


def test_taking_the_sun_out_loses_no_light():
    pixels = with_sun()
    lights, rest = environment.extract_lights(pixels)
    luminance = np.array([0.2126, 0.7152, 0.0722])
    in_lights = sum(np.dot(light.color, luminance) for light in lights) * math.pi
    assert in_lights + light_in(rest) == pytest.approx(light_in(pixels), rel=0.0001)


def test_a_sun_left_in_the_soft_light_gives_false_bands():
    """Why suns come out: left in, the side away from the sun gets negative light (dark bands)."""
    pixels = with_sun()
    d, _solid = environment.directions(32, 64)
    normals = d.reshape(-1, 3)
    plain = environment.irradiance(environment.project_harmonics(pixels), normals)
    _lights, rest = environment.extract_lights(pixels)
    without = environment.irradiance(environment.project_harmonics(rest), normals)
    assert plain.min() < -0.02 * plain.max()
    assert without.min() > 0


def test_several_lamps_become_several_lights():
    pixels = with_sun()
    pixels += with_sun(direction=(0.9, 0.2, 0.3), strength=9000.0)
    pixels += with_sun(direction=(-0.8, 0.1, 0.5), strength=6000.0)
    pixels += with_sun(direction=(-0.1, -0.9, 0.2), strength=5000.0)  # (a fourth: over the limit)
    lights, rest = environment.extract_lights(pixels)
    assert len(lights) == environment.MAX_LIGHTS
    strengths = [sum(light.color) for light in lights]
    assert strengths == sorted(strengths, reverse=True)  # (the strongest first: the viewer's key light)
    in_lights = sum(np.dot(light.color, [0.2126, 0.7152, 0.0722]) for light in lights) * math.pi
    assert in_lights + light_in(rest) == pytest.approx(light_in(pixels), rel=0.0001)  # (the fourth stays soft light)


def test_a_soft_sky_has_no_lights():
    d, _solid = environment.directions(128, 256)
    pixels = (0.3 + 0.5 * np.maximum(d[..., 1:2], 0)) * np.ones((128, 256, 3))
    assert environment.extract_lights(pixels)[0] == ()
    assert environment.extract_lights(np.zeros((64, 128, 3)))[0] == ()  # (all black: nothing breaks)


def test_exposure_brings_dark_and_bright_alike():
    dim, bright = environment.build(sky(value=0.01)), environment.build(sky(value=50.0))
    assert dim.exposure * 0.01 == pytest.approx(bright.exposure * 50.0, rel=0.01)
    assert dim.exposure * 0.01 == pytest.approx(0.45, rel=0.02)
    assert environment.build(np.zeros((64, 128, 3), dtype=np.float32)).exposure > 0  # (black: still a number)


def test_build_makes_a_texture_the_gpu_can_take():
    pixels = np.full((1024, 2048, 3), 0.5, dtype=np.float32)
    pixels[100, 100] = 900000.0  # (past what half floats hold)
    env = environment.build(pixels, r"D:\skies\noon_4k.hdr")
    assert env.pixels.dtype == np.float16 and env.pixels.shape == (1024, 2048, 3)
    assert np.isfinite(env.pixels).all()
    assert env.harmonics.shape == (9, 3) and env.name == "noon_4k"


# ----------------------------------------------------------------------
# Turning
# ----------------------------------------------------------------------
def test_the_environment_stays_put_as_the_camera_orbits():
    """A direction in the panorama's space, seen from the camera after it has orbited, and back."""
    still = environment.view_to_env(0.0, 0.0, 0.0)
    assert still == pytest.approx(np.eye(3))
    # The camera turned 90 degrees round the model: what was straight ahead is now to one side
    turned = environment.view_to_env(0.0, 90.0, 0.0)
    assert turned @ np.array([0.0, 0.0, -1.0]) == pytest.approx(np.array([1.0, 0.0, 0.0]), abs=1e-9)
    assert turned.T @ turned == pytest.approx(np.eye(3))


def test_auto_rotate_keeps_the_environment_still_on_screen():
    """The viewport's turntable orbits the camera and turns the panorama back by the same angle."""
    start = environment.view_to_env(20.0, -35.0, 10.0)
    assert environment.view_to_env(20.0, -35.0 + 57.0, 10.0 - 57.0) == pytest.approx(start)


@pytest.mark.parametrize("sun", [(0.0, 0.6, -0.8), (0.9, 0.2, 0.3), (-0.5, 0.1, 0.8)])
@pytest.mark.parametrize("rot_y", [-35.0, 120.0])
def test_an_hdri_starts_with_its_key_light_at_the_front_right(sun, rot_y):
    env = environment.build(with_sun(direction=sun).astype(np.float32))
    yaw = environment.key_light_yaw(env, rot_y)
    x, _y, z = np.array(env.lights[0].direction) @ environment.view_to_env(0.0, rot_y, yaw)
    assert x > 0 and z > 0  # (to the right, on the viewer's side)
    assert math.degrees(math.atan2(x, z)) == pytest.approx(environment.KEY_LIGHT_AZIMUTH, abs=0.01)


def test_a_soft_hdri_isnt_turned():
    assert environment.key_light_yaw(environment.build(sky()), -35.0) == 0.0


def test_reflections_blur_by_the_panoramas_size():
    assert environment.reflection_level(4.0, 2048) == 4.0
    assert environment.reflection_level(4.0, 1024) == 3.0  # (a level of a smaller image is as blurred)
    assert environment.reflection_level(0.5, 512) == 0.0


# ----------------------------------------------------------------------
# Reading and picking
# ----------------------------------------------------------------------
def panorama(height=256):
    return np.full((height, height * 2, 3), 0.4, dtype=np.float32)


def test_headers_say_what_an_image_is(tmp_path):
    exr = write_exr(tmp_path / "a.exr", panorama())
    hdr = write_hdr(tmp_path / "a.hdr", panorama())
    gray = write_exr(tmp_path / "b.exr", np.zeros((64, 128), dtype=np.float32), "Y")
    assert hdr_image.header_info(exr.read_bytes(), ".exr") == (512, 256, True)
    assert hdr_image.header_info(hdr.read_bytes(), ".hdr") == (512, 256, True)
    assert hdr_image.header_info(gray.read_bytes(), ".exr") == (128, 64, False)
    assert hdr_image.header_info(b"not an image", ".exr") is None
    assert hdr_image.header_info(b"not an image", ".hdr") is None
    assert hdr_image.header_info(exr.read_bytes()[:12], ".exr") is None  # (cut short)


def test_loading_is_full_resolution_and_once(tmp_path, monkeypatch):
    environment.forget()
    pixels = panorama()
    pixels[50, 70] = 9000.0  # (found however few pixels it is)
    path = write_exr(tmp_path / "noon.exr", pixels)
    env = environment.load(str(path))
    assert env.name == "noon" and env.pixels.shape == (256, 512, 3) and env.lights
    monkeypatch.setattr(hdr_image, "read_linear", lambda _path: pytest.fail("read twice"))
    assert environment.load(str(path)) is env


def test_loading_forgets_the_oldest(tmp_path):
    environment.forget()
    paths = [write_hdr(tmp_path / f"{i}.hdr", panorama()) for i in range(environment.KEPT + 1)]
    for path in paths:
        environment.load(str(path))
    assert len(environment._kept) == environment.KEPT and str(paths[0]) not in environment._kept


@pytest.mark.parametrize("make, reason", [
    (lambda p: write_exr(p / "bake.exr", np.zeros((512, 512, 3), dtype=np.float32)), "2:1"),
    (lambda p: write_exr(p / "height.exr", np.zeros((256, 512), dtype=np.float32), "Y"), "2:1"),
])
def test_images_that_cant_light_a_scene_say_so(tmp_path, make, reason):
    environment.forget()
    path = make(tmp_path)
    with pytest.raises(environment.NotAPanorama, match=reason):
        environment.load(str(path))
    with pytest.raises(environment.NotAPanorama, match=reason):
        environment.check(str(path))


def test_a_missing_or_damaged_file(tmp_path):
    with pytest.raises(OSError):
        environment.load(str(tmp_path / "gone.hdr"))
    with pytest.raises(environment.NotAPanorama):
        environment.check(str(tmp_path / "gone.hdr"))
    damaged = tmp_path / "damaged.hdr"
    damaged.write_bytes(b"not an image")
    with pytest.raises(environment.NotAPanorama, match="read"):
        environment.load(str(damaged))


def test_a_panorama_inside_an_archive(tmp_path):
    environment.forget()
    hdr = write_hdr(tmp_path / "noon.hdr", panorama())
    pack = tmp_path / "pack.zip"
    with zipfile.ZipFile(pack, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(hdr, "skies/noon.hdr")
    hdr.unlink()
    key = str(archives.virtual_path(pack, "skies/noon.hdr"))
    assert environment.display_name(key) == "noon"
    environment.check(key)
    assert environment.load(key).pixels.shape == (256, 512, 3)
    assert [p.key for p in environment.find_panoramas([key])] == [key]
    with pytest.raises(FileNotFoundError):
        environment.load(str(archives.virtual_path(pack, "skies/other.hdr")))


def test_only_panoramas_are_offered(tmp_path):
    keep = [write_hdr(tmp_path / "noon_4k.hdr", panorama()), write_exr(tmp_path / "Evening.exr", panorama())]
    hidden = tmp_path / ".vrayThumbs"
    hidden.mkdir()
    others = [
        write_exr(tmp_path / "beauty.exr", np.zeros((540, 960, 3), dtype=np.float32)),  # (a render)
        write_exr(tmp_path / "height.exr", np.zeros((256, 512), dtype=np.float32), "Y"),  # (grey: a height map)
        write_exr(tmp_path / "tiny.exr", np.zeros((128, 256, 3), dtype=np.float32)),  # (a renderer's thumbnail)
        write_exr(hidden / "sky.exr", panorama()),  # (... and where they keep them)
        tmp_path / "gone.hdr",
    ]
    found = environment.find_panoramas(str(p) for p in keep + others)
    assert [p.name for p in found] == ["Evening", "noon_4k"]  # (sorted, whatever the case)
    assert (found[1].width, found[1].height) == (512, 256)


def test_one_hdri_in_two_formats_is_listed_once(tmp_path):
    exr = write_exr(tmp_path / "noon.exr", panorama())
    hdr = write_hdr(tmp_path / "noon.hdr", panorama())
    other_size = tmp_path / "other"
    other_size.mkdir()
    bigger = write_hdr(other_size / "noon.hdr", panorama(512))  # (the same name, not the same image)
    found = environment.find_panoramas([str(exr), str(hdr), str(bigger)])
    assert [p.key for p in found] == [str(bigger), str(hdr)]  # (.hdr is the quicker to read)
