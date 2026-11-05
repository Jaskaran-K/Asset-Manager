"""What a model is really made of, as its file has it.

The viewer draws triangles, but a sculpt of 7 million quads isn't a model of 14 million triangles
to whoever made it. The quick readers (core.obj_fast, core.fbx_fast) fill this in as they read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass
class ModelStats:
    vertices: int = 0  # the points in the file (the viewer's own can be more: it splits them at hard edges)
    triangles: int = 0
    quads: int = 0
    ngons: int = 0  # five corners or more
    unit_cm: Optional[float] = None  # how many centimetres one of the file's units is, when it says

    @property
    def polygons(self) -> int:
        return self.triangles + self.quads + self.ngons

    def add_polygons(self, sizes: np.ndarray) -> None:
        """Count polygons, given how many corners each has (fewer than 3: not a polygon)."""
        self.triangles += int(np.count_nonzero(sizes == 3))
        self.quads += int(np.count_nonzero(sizes == 4))
        self.ngons += int(np.count_nonzero(sizes > 4))

    def add_fbx_polygons(self, polygon_vertex_index: np.ndarray) -> None:
        """Count polygons from an FBX's corner list, where a negative index ends each one."""
        ends = np.flatnonzero(np.asarray(polygon_vertex_index) < 0)
        self.add_polygons(np.diff(ends, prepend=-1))

    def summary(self) -> str:
        """The count for the viewer's badge: "7,079,846 quads", or "12,040 polygons" when they're mixed."""
        if self.polygons == self.quads:
            return f"{self.quads:,} quads"
        if self.polygons == self.triangles:
            return f"{self.triangles:,} tris"
        return f"{self.polygons:,} polygons"

    def rows(self, drawn_triangles: int) -> dict[str, str]:
        """For the viewer's details panel."""
        return {
            "Vertices": f"{self.vertices:,}",
            "Polygons": f"{self.polygons:,}",
            "Quads": f"{self.quads:,}",
            "Triangles": f"{self.triangles:,}",
            "N-gons": f"{self.ngons:,}",
            "Triangles When Drawn": f"{drawn_triangles:,}",
        }
