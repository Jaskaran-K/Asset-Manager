"""core.obj_fast: the quick OBJ reader (polygons, the file's normals, odd lines, big files in blocks)."""

from __future__ import annotations

import numpy as np
import pytest

from core import model_loader, obj_fast
from core.obj_fast import LoadCancelled, ObjError, read_obj

# A unit cube, quads, each face with its own normal (a hard edge all round)
CUBE = """# cube
o Cube
v -1 -1 -1
v 1 -1 -1
v 1 1 -1
v -1 1 -1
v -1 -1 1
v 1 -1 1
v 1 1 1
v -1 1 1
vt 0 0
vt 1 0
vt 1 1
vt 0 1
vn 0 0 -1
vn 0 0 1
vn 0 -1 0
vn 0 1 0
vn -1 0 0
vn 1 0 0
usemtl grey
f 1/1/1 4/4/1 3/3/1 2/2/1
f 5/1/2 6/2/2 7/3/2 8/4/2
f 1/1/3 2/2/3 6/3/3 5/4/3
f 4/1/4 8/2/4 7/3/4 3/4/4
f 1/1/5 5/2/5 8/3/5 4/4/5
f 2/1/6 3/2/6 7/3/6 6/4/6
"""


def write(tmp_path, text, name="model.obj", newline="\n"):
    path = tmp_path / name
    path.write_bytes(text.replace("\n", newline).encode("utf-8"))
    return path


def test_quads_become_triangles_and_hard_edges_keep_their_normals(tmp_path):
    vertices, faces, normals = read_obj(write(tmp_path, CUBE))
    assert faces.shape == (12, 3)
    assert len(vertices) == 24  # each corner with each of its 3 faces' normals
    assert normals is not None and np.allclose(np.linalg.norm(normals, axis=1), 1)
    for triangle in faces:  # every triangle's corners share its face's normal
        assert np.allclose(normals[triangle[0]], normals[triangle[1]]) and np.allclose(normals[triangle[0]], normals[triangle[2]])


def test_normals_numbered_per_corner_but_alike_dont_split_vertices(tmp_path):
    # A flat square written as many exporters do: one "vn" line per corner, all the same direction
    text = "v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\n" + "vn 0 0 1\n" * 6 + "f 1//1 2//2 3//3\nf 1//4 3//5 4//6\n"
    vertices, faces, normals = read_obj(write(tmp_path, text))
    assert len(vertices) == 4 and faces.shape == (2, 3) and np.allclose(normals, [0, 0, 1])


@pytest.mark.parametrize("corner", ["{v}", "{v}/{v}", "{v}//{v}", "{v}/{v}/{v}"])
def test_every_corner_form(tmp_path, corner):
    lines = ["v 0 0 0", "v 1 0 0", "v 1 1 0", "v 0 1 0", "vt 0 0", "vt 1 0", "vt 1 1", "vt 0 1"] + ["vn 0 0 1"] * 4
    lines.append("f " + " ".join(corner.format(v=i) for i in (1, 2, 3, 4)))
    vertices, faces, normals = read_obj(write(tmp_path, "\n".join(lines) + "\n"))
    assert sorted(map(tuple, faces.tolist())) == [(0, 1, 2), (0, 2, 3)]
    assert (normals is not None) == ("//" in corner or corner.count("/") == 2)


def test_tabs_crlf_colors_and_a_pentagon(tmp_path):
    text = "v\t0 0 0 1 0 0\nv 1 0 0 0 1 0\nv 1 1 0 0 0 1\nv 0.5 1.5 0 1 1 1\nv 0 1 0 1 1 1\nf\t1 2 3 4 5\n"
    vertices, faces, _normals = read_obj(write(tmp_path, text, newline="\r\n"))
    assert vertices.shape == (5, 3) and np.allclose(vertices[3], [0.5, 1.5, 0])  # (colors dropped)
    assert faces.tolist() == [[0, 1, 2], [0, 2, 3], [0, 3, 4]]  # a fan


def test_big_files_are_read_in_blocks(tmp_path, monkeypatch):
    import trimesh

    sphere = trimesh.creation.icosphere(subdivisions=4)
    path = tmp_path / "sphere.obj"
    sphere.export(path)
    monkeypatch.setattr(obj_fast, "BLOCK_BYTES", 4096)  # (dozens of blocks, lines cut at block ends)
    vertices, faces, _normals = read_obj(path)
    assert len(faces) == len(sphere.faces)
    assert np.allclose(np.sort(vertices[faces].reshape(-1, 9), axis=0), np.sort(sphere.vertices[sphere.faces].reshape(-1, 9), axis=0), atol=1e-5)


def test_reading_stops_when_asked(tmp_path, monkeypatch):
    path = write(tmp_path, CUBE * 20)
    monkeypatch.setattr(obj_fast, "BLOCK_BYTES", 256)
    asked = []
    with pytest.raises(LoadCancelled):
        read_obj(path, should_stop=lambda: asked.append(1) or len(asked) > 3)
    with pytest.raises(LoadCancelled):  # (the model loader lets it through rather than trying trimesh)
        model_loader.load_3d_model(path, should_stop=lambda: True)


def test_what_it_cant_follow_goes_to_trimesh(tmp_path):
    relative = "v 0 0 0\nv 1 0 0\nv 1 1 0\nf -3 -2 -1\n"  # (relative indices)
    with pytest.raises(ObjError):
        read_obj(write(tmp_path, relative))
    mesh = model_loader.load_3d_model(write(tmp_path, relative, "relative.obj"))
    assert mesh.face_count == 1 and not mesh.stand_in and "Quick Reader Note" in mesh.metadata
    with pytest.raises(ObjError):
        read_obj(write(tmp_path, "v 0 0 0\nv 1 0 0\n", "points.obj"))  # (no faces)


def test_the_model_loader_uses_it(tmp_path):
    mesh = model_loader.load_3d_model(write(tmp_path, CUBE))
    assert mesh.face_count == 12 and mesh.vertex_count == 24 and "Quick Reader Note" not in mesh.metadata
    assert np.allclose(np.abs(mesh.vertices).max(), 0.9)  # (centered and scaled for the viewport)
