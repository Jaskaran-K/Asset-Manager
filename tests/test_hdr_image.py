"""EXR and Radiance HDR images: reading, tone mapping, sizes from the header, and thumbnails."""

from __future__ import annotations

import numpy as np
import OpenEXR
import pytest

from core import hdr_image, metadata
from core.thumbnail_manager import load_texture_thumbnail_image, open_image


def write_exr(path, pixels, name="RGB"):
    header = {"compression": OpenEXR.ZIP_COMPRESSION, "type": OpenEXR.scanlineimage}
    with OpenEXR.File(header, {name: pixels}) as f:
        f.write(str(path))
    return path


def rgbe(rgb: np.ndarray) -> np.ndarray:
    """Float RGB (H x W x 3) as Radiance RGBE bytes (H x W x 4)."""
    brightest = rgb.max(axis=2)
    mantissa, exponent = np.frexp(brightest)
    scale = np.where(brightest > 1e-32, mantissa * 256.0 / np.maximum(brightest, 1e-32), 0)
    out = np.zeros(rgb.shape[:2] + (4,), np.uint8)
    out[..., :3] = np.clip(rgb * scale[..., None], 0, 255)
    out[..., 3] = np.where(brightest > 1e-32, exponent + 128, 0)
    return out


def rle(values: bytes) -> bytes:
    """One component of a scanline, run-length encoded as Radiance writes it."""
    out, i = bytearray(), 0
    while i < len(values):
        run = 1
        while i + run < len(values) and run < 127 and values[i + run] == values[i]:
            run += 1
        if run >= 4:
            out += bytes((128 + run, values[i]))
            i += run
            continue
        literal = i
        while literal < len(values) and literal - i < 128:
            if literal + 3 < len(values) and values[literal] == values[literal + 1] == values[literal + 2] == values[literal + 3]:
                break
            literal += 1
        out += bytes((literal - i,)) + values[i:literal]
        i = literal
    return bytes(out)


def write_hdr(path, rgb, compressed=True, flip=False):
    height, width = rgb.shape[:2]
    pixels = rgbe(rgb[::-1] if flip else rgb)
    body = bytearray()
    for row in pixels:
        if compressed:
            body += bytes((2, 2, width >> 8, width & 255))
            for component in range(4):
                body += rle(row[:, component].tobytes())
        else:
            body += row.tobytes()
    resolution = f"{'+' if flip else '-'}Y {height} +X {width}"
    path.write_bytes(b"#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n\n" + resolution.encode() + b"\n" + bytes(body))
    return path


def sky(height=24, width=48):
    """A dim ground under a bright sky with a very bright sun."""
    rgb = np.full((height, width, 3), 0.05, np.float32)
    rgb[: height // 2] = (0.4, 0.6, 1.5)
    rgb[2:5, 10:14] = 400.0  # (about 1% of the picture, as in a real HDRI)
    rgb[:, width // 2] *= 0.5  # (a stripe, to tell flips apart)
    return rgb


@pytest.mark.parametrize("compressed", [True, False])
def test_radiance_matches_exr(tmp_path, compressed):
    pixels = sky()
    from_hdr = np.asarray(hdr_image.read_hdr_image(write_hdr(tmp_path / "sky.hdr", pixels, compressed)))
    from_exr = np.asarray(hdr_image.read_hdr_image(write_exr(tmp_path / "sky.exr", pixels)))
    assert from_hdr.shape == from_exr.shape == (24, 48, 3)
    assert np.abs(from_hdr.astype(int) - from_exr.astype(int)).max() <= 3  # (RGBE keeps ~1% precision)


def test_hdris_are_tone_mapped(tmp_path):
    image = np.asarray(hdr_image.read_hdr_image(write_exr(tmp_path / "sky.exr", sky())))
    sky_blue, ground, sun = image[5, 5], image[20, 5], image[2, 10]
    assert sky_blue[2] < 255 and sky_blue[2] > sky_blue[0]  # the bright sky keeps its color, not clipped white
    assert sun.min() >= 250  # the sun is white
    assert 0 < ground.max() < sky_blue.max()


def test_data_maps_show_as_they_are_and_one_channel_maps_are_stretched(tmp_path):
    normal = np.zeros((8, 8, 3), np.float32)
    normal[...] = (0.5, 0.5, 1.0)
    image = np.asarray(hdr_image.read_hdr_image(write_exr(tmp_path / "normal.exr", normal)))
    assert tuple(image[0, 0]) == (188, 188, 255)  # 0.5 linear, shown in sRGB
    height = np.linspace(-2.0, 6.0, 64, dtype=np.float32).reshape(8, 8)
    image = hdr_image.read_hdr_image(write_exr(tmp_path / "height.exr", height, name="Y"))
    assert image.mode == "L" and np.asarray(image).min() <= 5 and np.asarray(image).max() >= 250


def test_flipped_files_and_alpha(tmp_path):
    pixels = sky()
    upright = np.asarray(hdr_image.read_hdr_image(write_hdr(tmp_path / "a.hdr", pixels)))
    flipped = np.asarray(hdr_image.read_hdr_image(write_hdr(tmp_path / "b.hdr", pixels, flip=True)))
    assert (upright == flipped).all()
    rgba = np.ones((4, 4, 4), np.float32)
    rgba[..., 3] = 0.25
    assert hdr_image.read_hdr_image(write_exr(tmp_path / "a.exr", rgba, name="RGBA")).getpixel((0, 0))[3] == 64


def test_sizes_come_from_the_header_and_thumbnails_are_subsampled(tmp_path, qapp):
    hdr = write_hdr(tmp_path / "wide.hdr", np.ones((40, 300, 3), np.float32))
    exr = write_exr(tmp_path / "wide.exr", np.ones((40, 300, 3), np.float32))
    for path in (hdr, exr):
        assert hdr_image.image_size(path) == metadata.image_size(str(path)) == (300, 40)
        small = open_image(path, max_size=100)
        assert 100 <= small.width < 300  # (every n-th pixel: the thumbnail's final resize does the rest)
        assert load_texture_thumbnail_image(path, path.suffix[1:].upper(), 110) is not None


def test_damaged_files_raise(tmp_path, qapp):
    good = write_hdr(tmp_path / "good.hdr", sky()).read_bytes()
    (tmp_path / "cut.hdr").write_bytes(good[: len(good) // 2])
    (tmp_path / "junk.exr").write_bytes(b"not an exr file")
    for name in ("cut.hdr", "junk.exr"):
        with pytest.raises(Exception):
            hdr_image.read_hdr_image(tmp_path / name)
        assert load_texture_thumbnail_image(tmp_path / name, "HDR", 110) is None


def test_non_ascii_paths(tmp_path):
    folder = tmp_path / "Ünïcode Földer"
    folder.mkdir()
    path = write_exr(folder / "sky.exr", sky())
    assert hdr_image.read_hdr_image(path).size == (48, 24)
