"""What a model is made of (core.model_stats): as the file has it, not the triangles it's drawn with."""

from __future__ import annotations

import numpy as np
import pytest

from core import fbx_fast, model_loader
from core.model_stats import ModelStats
from core.obj_fast import read_obj
from tests.test_model_loading import QUAD_AND_PENTAGON, write_fbx


def write_obj(path, faces, vertices=8):
    lines = [f"v {i % 2} {(i >> 1) % 2} {(i >> 2) % 2}" for i in range(vertices)] + [f"f {' '.join(map(str, f))}" for f in faces]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


MIXED = [(1, 2, 3), (1, 2, 3, 4), (5, 6, 7, 8), (1, 2, 3, 4, 5), (1, 2, 3, 4, 5, 6)]


def test_counting_polygons():
    stats = ModelStats()
    stats.add_polygons(np.array([3, 4, 4, 5, 6, 2, 1]))  # (under 3 corners: not a polygon)
    assert (stats.triangles, stats.quads, stats.ngons, stats.polygons) == (1, 2, 2, 5)


def test_counting_an_fbx_corner_list():
    stats = ModelStats()
    stats.add_fbx_polygons(np.array([0, 1, ~2, 0, 1, 2, ~3, 0, 1, 2, 3, ~4], dtype=np.int32))
    assert (stats.triangles, stats.quads, stats.ngons) == (1, 1, 1)


@pytest.mark.parametrize("triangles, quads, ngons, text", [
    (0, 7_079_846, 0, "7,079,846 quads"),
    (12, 0, 0, "12 tris"),
    (4, 7_079_846, 0, "7,079,850 polygons"),
    (0, 0, 3, "3 polygons"),
])
def test_the_viewers_badge(triangles, quads, ngons, text):
    assert ModelStats(triangles=triangles, quads=quads, ngons=ngons).summary() == text


def test_an_obj_says_what_it_holds(tmp_path):
    stats = ModelStats()
    _vertices, faces, _normals = read_obj(write_obj(tmp_path / "m.obj", MIXED), stats=stats)
    assert (stats.triangles, stats.quads, stats.ngons, stats.vertices) == (1, 2, 2, 8)
    assert len(faces) == 1 + 2 * 2 + 3 + 4  # (what's drawn: triangles)


def test_read_obj_works_without_stats(tmp_path):
    assert len(read_obj(write_obj(tmp_path / "m.obj", MIXED))[1]) == 12


def test_hard_edges_dont_add_to_the_vertices_shown(tmp_path):
    """The viewer splits points at hard edges to draw them: the model still has the points it has."""
    from tests.test_obj_fast import CUBE

    mesh = model_loader.load_3d_model(_written(tmp_path, CUBE))
    assert mesh.vertex_count == 24 and mesh.stats.vertices == 8
    assert mesh.metadata["Vertices"] == "8"


def _written(tmp_path, text):
    path = tmp_path / "cube.obj"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_details_of_an_obj(tmp_path):
    mesh = model_loader.load_3d_model(write_obj(tmp_path / "m.obj", MIXED))
    assert mesh.stats.polygons == 5
    meta = mesh.metadata
    assert (meta["Polygons"], meta["Quads"], meta["Triangles"], meta["N-gons"]) == ("5", "2", "1", "2")
    assert meta["Triangles When Drawn"] == "12" and "Triangles / Faces" not in meta
    assert meta["Dimensions (X×Y×Z)"] == "1.00 × 1.00 × 1.00 units"  # (an OBJ doesn't say what its numbers mean)


def test_an_fbx_says_what_it_holds(tmp_path):
    polygons = [[0, 1, 2, 3], [4, 5, 6]]
    path = write_fbx(tmp_path / "m.fbx", [(1, QUAD_AND_PENTAGON, polygons, (0, 0, 0), 0)])
    mesh = model_loader.load_3d_model(path)
    assert (mesh.stats.triangles, mesh.stats.quads, mesh.stats.ngons, mesh.stats.vertices) == (1, 1, 0, 7)
    assert mesh.metadata["Polygons"] == "2" and mesh.metadata["Triangles When Drawn"] == "3"


def test_every_mesh_of_an_fbx_is_counted(tmp_path):
    meshes = [(i, QUAD_AND_PENTAGON, [[0, 1, 2, 3]], (0, 0, 0), 0) for i in (1, 3)]
    stats = ModelStats()
    fbx_fast.read_fbx(write_fbx(tmp_path / "two.fbx", meshes), stats=stats)
    assert stats.quads == 2 and stats.vertices == 14


@pytest.mark.parametrize("factor, shown", [(1.0, "3.00 × 2.00 × 0.00 cm"), (100.0, "300.00 × 200.00 × 0.00 cm"), (2.54, "7.62 × 5.08 × 0.00 cm")])
def test_an_fbx_says_its_unit(tmp_path, factor, shown):
    path = write_fbx(tmp_path / "m.fbx", [(1, QUAD_AND_PENTAGON, [[0, 1, 2, 3], [4, 5, 6]], (0, 0, 0), 0)], unit_scale=factor)
    mesh = model_loader.load_3d_model(path)
    assert mesh.stats.unit_cm == factor and mesh.metadata["Dimensions (X×Y×Z)"] == shown


@pytest.mark.parametrize("factor", [0.0, -1.0, "cm"])
def test_a_wrong_unit_isnt_believed(tmp_path, factor):
    path = write_fbx(tmp_path / "m.fbx", [(1, QUAD_AND_PENTAGON, [[0, 1, 2, 3]], (0, 0, 0), 0)], unit_scale=factor)
    mesh = model_loader.load_3d_model(path)
    assert mesh.stats.unit_cm is None and mesh.metadata["Dimensions (X×Y×Z)"].endswith(" units")


def test_a_file_only_trimesh_can_read_shows_triangles(tmp_path):
    from tests.test_obj_fast import CUBE  # noqa: F401

    relative = "v 0 0 0\nv 1 0 0\nv 0 1 0\nf -3 -2 -1\n"  # (relative indices: the quick reader passes it on)
    mesh = model_loader.load_3d_model(_written(tmp_path, relative))
    assert mesh.stats is None and mesh.metadata["Triangles / Faces"] == "1" and mesh.metadata["Vertices"] == "3"


def test_ascii_fbx_counts_too(tmp_path):
    text = (
        "; FBX 7.4.0 project file\n"
        "Vertices: *12 {\n a: 0,0,0,1,0,0,1,1,0,0,1,0\n}\n"
        "PolygonVertexIndex: *4 {\n a: 0,1,2,-4\n}\n"
    )
    path = tmp_path / "ascii.fbx"
    path.write_text(text, encoding="utf-8")
    mesh = model_loader.load_3d_model(path)
    assert mesh.metadata["Subtype"] == "ASCII FBX" and mesh.stats.quads == 1 and mesh.stats.polygons == 1


def test_details_for_search_still_count_triangles(tmp_path):
    """poly:<10k means what it did: what's drawn."""
    from core.metadata import read_details

    assert read_details(str(write_obj(tmp_path / "m.obj", MIXED)), ".obj").poly_count == 12
