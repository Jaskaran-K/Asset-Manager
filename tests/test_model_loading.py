"""3D models: the quick binary FBX reader (core.fbx_fast), normals, how models are shaded
(core.model_look) and their rendered thumbnails (core.model_thumbnail)."""

from __future__ import annotations

import struct
import zlib

import numpy as np
import pytest

from core import fbx_fast, model_loader
from core.model_look import LIGHTINGS, MATERIALS, light_directions, shade


# ----------------------------------------------------------------------
# A small binary FBX writer (just what the readers need)
# ----------------------------------------------------------------------
def _prop(value) -> bytes:
    if isinstance(value, str):
        raw = value.encode("utf-8")
        return b"S" + struct.pack("<I", len(raw)) + raw
    if isinstance(value, float):
        return b"D" + struct.pack("<d", value)
    if isinstance(value, int):
        return b"L" + struct.pack("<q", value)
    kind, array, compress = value  # ("d" / "i", numpy array, zipped?)
    raw = array.astype({"d": "<f8", "i": "<i4"}[kind]).tobytes()
    body = zlib.compress(raw) if compress else raw
    return kind.encode() + struct.pack("<III", len(array), 1 if compress else 0, len(body)) + body


def _node(name: str, props: list, children: list, at: int, wide: bool) -> bytes:
    head = struct.Struct("<QQQ" if wide else "<III")
    prop_bytes = b"".join(_prop(p) for p in props)
    start = at + head.size + 1 + len(name) + len(prop_bytes)
    body = b""
    for child_name, child_props, grandchildren in children:
        body += _node(child_name, child_props, grandchildren, start + len(body), wide)
    if children:
        body += b"\0" * (head.size + 1)
    end = start + len(body)
    return head.pack(end, len(props), len(prop_bytes)) + bytes([len(name)]) + name.encode() + prop_bytes + body


def _uv_layer(number, layer, compress):
    name, uvs, index, mapping, reference = layer
    children = [("Name", [name], []), ("MappingInformationType", [mapping], []), ("ReferenceInformationType", [reference], []),
                ("UV", [("d", uvs.reshape(-1), compress)], [])]
    if index is not None:
        children.append(("UVIndex", [("i", index, compress)], []))
    return "LayerElementUV", [number], children


def write_fbx(path, meshes, version=7400, compress=True, unit_scale=None, uv_sets=None, orphan_geometry=False):
    """meshes: [(id, vertices N x 3, polygons [[i, j, k, ...]], translation, parent id or 0)].
    unit_scale: the file's UnitScaleFactor (how many cm one of its units is), if it's to say.
    uv_sets: {mesh id: [(name, UVs N x 2, UV index or None, mapping, reference)]}.
    orphan_geometry: its meshes aren't connected to their models (not in the scene)."""
    wide = version >= 7500
    objects, connections = [], []
    for mesh_id, vertices, polygons, translation, parent in meshes:
        index = []
        for polygon in polygons:
            index += list(polygon[:-1]) + [~polygon[-1]]
        objects.append(("Geometry", [mesh_id + 1, "Geometry::m\x00\x01Geometry", "Mesh"], [
            ("Vertices", [("d", np.asarray(vertices, dtype=float).reshape(-1), compress)], []),
            ("PolygonVertexIndex", [("i", np.asarray(index), compress)], []),
            *[_uv_layer(number, layer, compress) for number, layer in enumerate((uv_sets or {}).get(mesh_id, []))],
        ]))
        p70 = [("P", ["Lcl Translation", "Lcl Translation", "", "A", *map(float, translation)], [])]
        objects.append(("Model", [mesh_id, "Model::m\x00\x01Model", "Mesh"], [("Properties70", [], p70)]))
        if not orphan_geometry:
            connections.append(("C", ["OO", mesh_id + 1, mesh_id], []))
        connections.append(("C", ["OO", mesh_id, parent], []))
    data = b"Kaydara FBX Binary  \x00\x1a\x00" + struct.pack("<I", version)
    settings = [("Properties70", [], [("P", ["UnitScaleFactor", "double", "Number", "", unit_scale], [])])] if unit_scale is not None else []
    for name, children in (("GlobalSettings", settings), ("Objects", objects), ("Connections", connections)):
        data += _node(name, [], children, len(data), wide)
    data += b"\0" * (25 if wide else 13) + b"\0" * 176
    path.write_bytes(data)
    return path


QUAD_AND_PENTAGON = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [2, 0, 0], [3, 1, 0], [2, 2, 0]], dtype=float)


@pytest.mark.parametrize("version, compress", [(7400, True), (7400, False), (7500, True)])
def test_the_quick_reader_matches_fbxloader(tmp_path, version, compress):
    import fbxloader

    path = write_fbx(tmp_path / "parts.fbx", [
        (100, QUAD_AND_PENTAGON, [[0, 1, 2, 3], [1, 4, 5, 6, 2]], (0, 0, 0), 0),
        (200, np.array([[0, 0, 0], [1, 0, 0], [0, 0, 1]], dtype=float), [[0, 1, 2]], (5, 0, 0), 100),  # (a child, moved)
    ], version, compress)
    vertices, faces, meshes = fbx_fast.read_fbx(path)
    assert meshes == 2 and faces.dtype == np.uint32 and vertices.dtype == np.float32
    expected = fbxloader.FBXLoader(str(path)).export_trimesh()
    # Same triangles, at the same places (fbxloader's trimesh merges duplicate points: compare corners)
    assert len(faces) == len(expected.faces) == 2 + 3 + 1
    np.testing.assert_allclose(np.sort(vertices[faces].reshape(len(faces), -1), axis=0),
                               np.sort(np.asarray(expected.vertices)[expected.faces].reshape(len(faces), -1), axis=0), atol=1e-5)
    assert vertices[faces[-1]][:, 0].max() == pytest.approx(6.0)  # (the child is 5 to the right)


def test_triangulating_polygons():
    #   a quad, a triangle and a pentagon, as FBX writes them (~ marks each one's last corner)
    index = np.array([0, 1, 2, ~3, 4, 5, ~6, 7, 8, 9, 10, ~11], dtype=np.int32)
    faces = fbx_fast.triangulate(index)
    assert faces.tolist() == [[0, 1, 2], [0, 2, 3], [4, 5, 6], [7, 8, 9], [7, 9, 10], [7, 10, 11]]
    assert fbx_fast.triangulate(np.array([], dtype=np.int32)).shape == (0, 3)


def test_files_the_quick_reader_cant_read_fall_back(tmp_path):
    with pytest.raises(fbx_fast.FbxError):
        fbx_fast.read_fbx(write_fbx(tmp_path / "empty.fbx", []))  # (no meshes)
    ascii_fbx = tmp_path / "ascii.fbx"
    ascii_fbx.write_text(
        "Vertices: *9 { a: 0,0,0,1,0,0,0,1,0 }\nPolygonVertexIndex: *3 { a: 0,1,-3 }\n", encoding="utf-8"
    )
    mesh = model_loader.load_3d_model(ascii_fbx)
    assert mesh.face_count == 1 and mesh.metadata["Subtype"] == "ASCII FBX" and not mesh.stand_in
    broken = tmp_path / "broken.fbx"
    broken.write_bytes(b"Kaydara FBX Binary  \x00\x1a\x00" + struct.pack("<I", 7400) + b"\xff" * 40)
    mesh = model_loader.load_3d_model(broken)
    assert mesh.stand_in and "Binary Loader Note" in mesh.metadata


def test_loading_an_fbx_gives_smooth_normals(tmp_path):
    path = write_fbx(tmp_path / "quad.fbx", [(100, QUAD_AND_PENTAGON[:4], [[0, 1, 2, 3]], (0, 0, 0), 0)])
    mesh = model_loader.load_3d_model(path)
    assert mesh.face_count == 2 and mesh.metadata["Subtype"] == "Binary FBX" and "Meshes" not in mesh.metadata
    np.testing.assert_allclose(np.abs(mesh.normals[:, 2]), 1.0, atol=1e-6)  # (a flat quad faces along z)
    assert np.abs(mesh.vertices).max() == pytest.approx(0.9)  # (fitted to the viewport)


def test_normals_match_the_slow_way():
    rng = np.random.default_rng(3)
    vertices = rng.normal(size=(300, 3)).astype(np.float32)
    faces = rng.integers(0, 300, size=(1000, 3)).astype(np.uint32)
    original = model_loader._NORMALS_CHUNK
    try:
        model_loader._NORMALS_CHUNK = 128  # (several chunks)
        quick = model_loader._compute_vertex_normals(vertices, faces)
    finally:
        model_loader._NORMALS_CHUNK = original
    face_normals = np.cross(vertices[faces[:, 1]] - vertices[faces[:, 0]], vertices[faces[:, 2]] - vertices[faces[:, 0]])
    face_normals /= np.maximum(np.linalg.norm(face_normals, axis=1, keepdims=True), 1e-12)
    slow = np.zeros_like(vertices)
    for corner in range(3):
        np.add.at(slow, faces[:, corner], face_normals)
    slow /= np.maximum(np.linalg.norm(slow, axis=1, keepdims=True), 1e-12)
    used = np.unique(faces)
    np.testing.assert_allclose(quick[used], slow[used], atol=1e-4)


# ----------------------------------------------------------------------
# Materials and lighting
# ----------------------------------------------------------------------
def _sphere_points(count=2000):
    rng = np.random.default_rng(0)
    n = rng.normal(size=(count, 3))
    n /= np.linalg.norm(n, axis=1, keepdims=True)
    return n[n[:, 2] > 0.05]  # (the side facing the viewer)


def test_materials_shade_as_named():
    n = _sphere_points()
    eye = np.tile([0.0, 0.0, 1.0], (len(n), 1))
    gray = (0.5, 0.5, 0.5)
    studio = LIGHTINGS["studio"]
    matte = shade(n, eye, gray, MATERIALS["matte"], studio)
    glossy = shade(n, eye, gray, MATERIALS["glossy"], studio)
    assert glossy.max() > matte.max() + 0.2  # (highlights)
    red = shade(n, eye, (0.8, 0.1, 0.1), MATERIALS["metallic"], studio)
    assert (red[:, 0] >= red[:, 1]).all()  # (metal's highlights take its colour)
    toon = shade(n, eye, gray, MATERIALS["toon"], studio)
    assert len(np.unique(np.round(toon, 3), axis=0)) < 40  # (a few flat steps, not a smooth blend)
    normals = shade(n, eye, gray, MATERIALS["normals"], studio)
    np.testing.assert_allclose(normals, n * 0.5 + 0.5)
    for lighting in LIGHTINGS.values():  # every set-up lights something, none blows out
        lit = shade(n, eye, gray, MATERIALS["phong"], lighting)
        assert 0.05 < lit.mean() < 0.9, lighting.label


def test_turning_the_lights():
    studio = LIGHTINGS["studio"]
    key = light_directions(studio)[0]
    turned = light_directions(studio, yaw=180.0)[0]
    assert turned[0] == pytest.approx(-key[0]) and turned[2] == pytest.approx(-key[2]) and turned[1] == pytest.approx(key[1])
    assert light_directions(studio, pitch=-30.0)[0][1] > key[1]  # (tipped up)
    # A light from above, tipped toward the viewer, then swung to the right: the right side lights up
    top = LIGHTINGS["top"]
    tipped = light_directions(top, pitch=60.0)[0]
    assert tipped[2] > 0.8
    swung = light_directions(top, yaw=90.0, pitch=60.0)[0]
    assert swung[0] > 0.8 and abs(swung[2]) < 0.2
    n = _sphere_points()
    eye = np.tile([0.0, 0.0, 1.0], (len(n), 1))
    right = n[:, 0] > 0.5
    before = shade(n, eye, (0.5, 0.5, 0.5), MATERIALS["matte"], top, pitch=60.0)[right].mean()
    after = shade(n, eye, (0.5, 0.5, 0.5), MATERIALS["matte"], top, yaw=90.0, pitch=60.0)[right].mean()
    assert after > before + 0.05


# ----------------------------------------------------------------------
# Thumbnails
# ----------------------------------------------------------------------
def test_a_rendered_thumbnail(qapp):
    from core.model_thumbnail import render_model

    import trimesh

    sphere = trimesh.creation.icosphere(subdivisions=3)
    mesh = model_loader._normalize_geometry("ball", np.asarray(sphere.vertices), np.asarray(sphere.faces), None, "OBJ", {})
    image = render_model(mesh.vertices, mesh.faces, mesh.normals, 80)
    assert (image.width(), image.height()) == (80, 80)
    assert image.pixelColor(0, 0).alpha() == 0 and image.pixelColor(40, 40).alpha() == 255  # (see-through around it)
    holes = [(x, y) for x in range(28, 52) for y in range(28, 52) if image.pixelColor(x, y).alpha() < 255]
    assert not holes  # (big triangles are filled in: no gaps)
    # A big triangle, many pixels across, is filled too
    big = render_model(np.array([[-1, -1, 0], [1, -1, 0], [0, 1, 0]], dtype=np.float32), np.array([[0, 1, 2]]), None, 60)
    assert big.pixelColor(30, 40).alpha() == 255
    assert render_model(np.zeros((0, 3)), np.zeros((0, 3), dtype=np.uint32), None, 60) is None
