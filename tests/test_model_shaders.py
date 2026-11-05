"""The viewer's shaders on a real GPU, drawing into an offscreen buffer: no window opens.

The rest of the tests run without OpenGL, so a typing mistake in a shader would pass them and leave
every model black. This runs in a process of its own (the tests' offscreen platform has no OpenGL),
and skips on a computer without OpenGL 3.3, such as the GitHub runners. It draws a sphere through
the viewport's own shaders and looks at the pixels.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

NO_OPENGL = 77
PROGRAM = r"""
import math, sys
import numpy as np
from OpenGL import GL
from PySide6.QtGui import QGuiApplication, QOffscreenSurface, QOpenGLContext, QSurfaceFormat
from PySide6.QtOpenGL import QOpenGLFramebufferObject, QOpenGLFramebufferObjectFormat, QOpenGLShader, QOpenGLShaderProgram

app = QGuiApplication([])
fmt = QSurfaceFormat()
fmt.setVersion(3, 3)
fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
surface = QOffscreenSurface()
surface.setFormat(fmt)
surface.create()
context = QOpenGLContext()
context.setFormat(fmt)
if not context.create() or not context.makeCurrent(surface) or context.format().version() < (3, 3):
    sys.exit(77)

from core import environment
from core.model_look import MATERIALS, MAX_LIGHTS, with_roughness
from core.model_uvs import build_layout
from ui import uv_view, viewer


def compile_program(vertex, fragment):
    program = QOpenGLShaderProgram()
    assert program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, vertex), program.log()
    assert program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, fragment), program.log()
    assert program.link(), program.log()
    return program


def sphere(stacks=48, slices=96):
    theta = np.linspace(0, math.pi, stacks + 1)[:, None]
    phi = np.linspace(0, 2 * math.pi, slices + 1)[None, :]
    points = np.stack([np.sin(theta) * np.sin(phi), np.repeat(np.cos(theta), slices + 1, axis=1), np.sin(theta) * np.cos(phi)], axis=-1).reshape(-1, 3)
    grid = np.arange((stacks + 1) * (slices + 1)).reshape(stacks + 1, slices + 1)
    quads = np.stack([grid[:-1, :-1], grid[1:, :-1], grid[1:, 1:], grid[:-1, :-1], grid[1:, 1:], grid[:-1, 1:]], axis=-1).reshape(-1, 3)
    return points.astype(np.float32), quads.astype(np.uint32)


SIZE = 128
points, faces = sphere()
vao = GL.glGenVertexArrays(1)
GL.glBindVertexArray(vao)
for location in (0, 1):
    buffer = GL.glGenBuffers(1)
    GL.glBindBuffer(GL.GL_ARRAY_BUFFER, buffer)
    GL.glBufferData(GL.GL_ARRAY_BUFFER, points.nbytes, points, GL.GL_STATIC_DRAW)
    GL.glEnableVertexAttribArray(location)
    GL.glVertexAttribPointer(location, 3, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
elements = GL.glGenBuffers(1)
GL.glBindBuffer(GL.GL_ELEMENT_ARRAY_BUFFER, elements)
GL.glBufferData(GL.GL_ELEMENT_ARRAY_BUFFER, faces.nbytes, faces, GL.GL_STATIC_DRAW)
GL.glBindVertexArray(0)

framebuffer_format = QOpenGLFramebufferObjectFormat()
framebuffer_format.setAttachment(QOpenGLFramebufferObject.Attachment.Depth)
framebuffer_format.setInternalTextureFormat(GL.GL_RGBA8)
target = QOpenGLFramebufferObject(SIZE, SIZE, framebuffer_format)
assert target.isValid()
mesh = compile_program(viewer._MESH_VERT_SHADER, viewer._MESH_FRAG_SHADER)
backdrop = compile_program(viewer._BACKDROP_VERT_SHADER, viewer._BACKDROP_FRAG_SHADER)
backdrop_vao = GL.glGenVertexArrays(1)
to_clip = np.diag(np.array([0.8, 0.8, -0.8, 1.0], dtype=np.float32))  # (flat, with what's nearest the viewer nearest)
to_view = np.eye(4, dtype=np.float32)
to_view[2, 3] = -3.0  # (the viewer is out along +z, as in the viewport)


def upload(env):
    pixels = env.pixels
    texture = GL.glGenTextures(1)
    GL.glBindTexture(GL.GL_TEXTURE_2D, texture)
    GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
    GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGB16F, pixels.shape[1], pixels.shape[0], 0, GL.GL_RGB, GL.GL_HALF_FLOAT, pixels)
    GL.glGenerateMipmap(GL.GL_TEXTURE_2D)
    GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR_MIPMAP_LINEAR)
    GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_REPEAT)
    return texture


def env_uniforms(program, env, exposure):
    GL.glUniform1i(program.uniformLocation("uEnv"), 0)
    GL.glUniformMatrix3fv(program.uniformLocation("uViewToEnv"), 1, GL.GL_TRUE, np.eye(3, dtype=np.float32))
    GL.glUniform1f(program.uniformLocation("uExposure"), exposure)


def draw_sphere(env, material, color=(0.5, 0.5, 0.5)):
    # Gives the middle of the sphere and its top and bottom, right and left edges, as 0-255 colours
    target.bind()
    GL.glViewport(0, 0, SIZE, SIZE)
    GL.glEnable(GL.GL_DEPTH_TEST)
    GL.glClearColor(0, 0, 0, 1)
    GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
    mesh.bind()
    GL.glUniformMatrix4fv(mesh.uniformLocation("uMVP"), 1, GL.GL_TRUE, to_clip)
    GL.glUniformMatrix4fv(mesh.uniformLocation("uModelView"), 1, GL.GL_TRUE, to_view)
    GL.glUniformMatrix3fv(mesh.uniformLocation("uNormalMatrix"), 1, GL.GL_TRUE, np.eye(3, dtype=np.float32))
    u = mesh.uniformLocation
    GL.glUniform3f(u("uColor"), *color)
    GL.glUniform1f(u("uUnlit"), 0.0)
    GL.glUniform1f(u("uUseEnv"), 1.0)
    env_uniforms(mesh, env, env.exposure)
    GL.glUniform3fv(u("uSH"), 9, env.harmonics)
    level = environment.reflection_level(material.reflection_blur, env.pixels.shape[1])
    GL.glUniform1f(u("uEnvLevel"), level)
    GL.glUniform1f(u("uEnvSpread"), environment.reflection_spread(level, env.pixels.shape[1]))
    GL.glUniform1f(u("uRoughness"), material.roughness)
    dirs, colors = np.zeros((MAX_LIGHTS, 3), np.float32), np.zeros((MAX_LIGHTS, 3), np.float32)
    for i, light in enumerate(env.lights):
        dirs[i], colors[i] = light.direction, light.color
    GL.glUniform1i(u("uLightCount"), len(env.lights))
    GL.glUniform3fv(u("uLightDir"), MAX_LIGHTS, dirs)
    GL.glUniform3fv(u("uLightColor"), MAX_LIGHTS, colors)
    for name, value in (("uDiffuse", material.diffuse), ("uWrap", material.wrap), ("uSpecular", material.specular if material.env_specular is None else material.env_specular), ("uShininess", material.shininess),
                        ("uPhong", float(material.phong)), ("uMetal", material.metal), ("uRim", material.rim), ("uBands", float(material.bands)),
                        ("uShowNormals", float(material.normals))):
        GL.glUniform1f(u(name), value)
    GL.glBindVertexArray(vao)
    GL.glDrawElements(GL.GL_TRIANGLES, faces.size, GL.GL_UNSIGNED_INT, None)
    GL.glBindVertexArray(0)
    assert GL.glGetError() == 0
    image = np.frombuffer(GL.glReadPixels(0, 0, SIZE, SIZE, GL.GL_RGB, GL.GL_UNSIGNED_BYTE), np.uint8).reshape(SIZE, SIZE, 3)[::-1].astype(float)
    target.release()
    global last_image
    last_image = image
    mid, near = SIZE // 2, 16
    return {"middle": image[mid, mid], "top": image[near + 4, mid], "bottom": image[-near - 4, mid], "right": image[mid, -near - 4], "left": image[mid, near + 4], "corner": image[1, 1]}


# Soft light from all round: every side of a matte sphere is lit alike, at a sensible grey
even = environment.build(np.full((256, 512, 3), 0.8, dtype=np.float32))
GL.glActiveTexture(GL.GL_TEXTURE0)
upload(even)
matte = draw_sphere(even, MATERIALS["matte"])
for side in ("middle", "top", "bottom", "right", "left"):
    assert abs(matte[side][1] - matte["middle"][1]) < 2, ("not even", side, matte)
assert 90 < matte["middle"][1] < 200, matte
assert matte["corner"][1] == 0, "the sphere covered the corner"

# A sun straight above (panorama space = view space here): the top is lit, the bottom isn't
d, _solid = environment.directions(256, 512)
pixels = np.full((256, 512, 3), 0.05, dtype=np.float32)
pixels[d[..., 1] > 0.995] = 5000.0
sunny = environment.build(pixels)
assert len(sunny.lights) == 1 and sunny.lights[0].direction[1] > 0.99
upload(sunny)
clay = draw_sphere(sunny, MATERIALS["matte"])
assert clay["top"][1] > clay["middle"][1] + 20 and clay["middle"][1] > clay["bottom"][1], clay

# A warm sky above and a cool floor: the reflections show the panorama the right way up
half = np.zeros((256, 512, 3), dtype=np.float32)
half[:128] = (0.8, 0.2, 0.1)
half[128:] = (0.1, 0.2, 0.8)
split = environment.build(half)
upload(split)
metal = draw_sphere(split, MATERIALS["metallic"], color=(1.0, 1.0, 1.0))
assert metal["top"][0] > metal["top"][2] and metal["bottom"][2] > metal["bottom"][0], metal

# Roughness: from a mirror, where sky meets floor on one sharp line, to a soft blend
def sharpest_step(roughness):
    draw_sphere(split, with_roughness(MATERIALS["metallic"], roughness), color=(1.0, 1.0, 1.0))
    column = last_image[20:-20, SIZE // 2, 0]
    return np.abs(np.diff(column)).max()


mirror_step, soft_step = sharpest_step(0.0), sharpest_step(1.0)
assert mirror_step > soft_step * 2, (mirror_step, soft_step)
assert sharpest_step(0.5) < mirror_step, "the slider's middle is still a mirror"

# Every material draws something, and Normals is untouched by the HDRI
for name, material in MATERIALS.items():
    assert draw_sphere(sunny, material)["middle"].sum() > 0, name
normals = draw_sphere(sunny, MATERIALS["normals"])
assert abs(normals["middle"][2] - 255) < 3 and abs(normals["middle"][0] - 127) < 3, normals

# The backdrop: the middle of the window looks straight ahead, into the middle of the panorama
ahead = np.full((256, 512, 3), 0.0, dtype=np.float32)
ahead[d[..., 2] < 0] = (0.0, 0.8, 0.0)
scene = environment.build(ahead)
upload(scene)
target.bind()
GL.glViewport(0, 0, SIZE, SIZE)
GL.glClearColor(0, 0, 0, 1)
GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
backdrop.bind()
env_uniforms(backdrop, scene, 1.0)
half_view = math.tan(math.radians(viewer._FIELD_OF_VIEW / 2))
GL.glUniform2f(backdrop.uniformLocation("uViewScale"), half_view, half_view)
GL.glDisable(GL.GL_DEPTH_TEST)
GL.glBindVertexArray(backdrop_vao)
GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)
GL.glBindVertexArray(0)
assert GL.glGetError() == 0
image = np.frombuffer(GL.glReadPixels(0, 0, SIZE, SIZE, GL.GL_RGB, GL.GL_UNSIGNED_BYTE), np.uint8).reshape(SIZE, SIZE, 3)
assert image[:, :, 1].min() > 150 and image[:, :, 0].max() < 40, "the backdrop looks the wrong way"
target.release()

# The UV view's own shader, drawing lines where they should be, in solid alpha
uv_program = compile_program(uv_view._VERT_SHADER, uv_view._FRAG_SHADER)
layout = build_layout(np.array([[0.25, 0.5], [0.75, 0.5]], dtype=np.float32), np.array([0, 1]), np.array([2]))
line_vao = GL.glGenVertexArrays(1)
GL.glBindVertexArray(line_vao)
buffer = GL.glGenBuffers(1)
GL.glBindBuffer(GL.GL_ARRAY_BUFFER, buffer)
GL.glBufferData(GL.GL_ARRAY_BUFFER, layout.uvs.nbytes, layout.uvs, GL.GL_STATIC_DRAW)
GL.glEnableVertexAttribArray(0)
GL.glVertexAttribPointer(0, 2, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
line_elements = GL.glGenBuffers(1)
GL.glBindBuffer(GL.GL_ELEMENT_ARRAY_BUFFER, line_elements)
GL.glBufferData(GL.GL_ELEMENT_ARRAY_BUFFER, layout.edges.nbytes, layout.edges, GL.GL_STATIC_DRAW)
target.bind()
GL.glViewport(0, 0, SIZE, SIZE)
GL.glClearColor(0, 0, 0, 1)
GL.glClear(GL.GL_COLOR_BUFFER_BIT)
GL.glEnable(GL.GL_BLEND)
GL.glBlendFuncSeparate(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA, GL.GL_ONE, GL.GL_ONE_MINUS_SRC_ALPHA)
uv_program.bind()
GL.glUniform2f(uv_program.uniformLocation("uScale"), 2.0, 2.0)
GL.glUniform2f(uv_program.uniformLocation("uCenter"), 0.5, 0.5)
GL.glUniform4f(uv_program.uniformLocation("uColor"), 0.0, 1.0, 0.0, 0.5)
GL.glDrawElements(GL.GL_LINES, 2, GL.GL_UNSIGNED_INT, None)
assert GL.glGetError() == 0
pixels = np.frombuffer(GL.glReadPixels(0, 0, SIZE, SIZE, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE), np.uint8).reshape(SIZE, SIZE, 4)
rows, columns = np.nonzero(pixels[:, :, 1])
assert len(rows) and SIZE * 0.2 < columns.min() < SIZE * 0.3 and SIZE * 0.7 < columns.max() < SIZE * 0.8, "the line isn't where its UVs say"
assert abs(rows.mean() - SIZE / 2) < 2 and (pixels[:, :, 3] == 255).all(), "lines made the window see-through"
target.release()
print("ok")
context.doneCurrent()
"""


def test_the_shaders_draw_what_they_should(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
    root = Path(__file__).resolve().parent.parent
    env["PYTHONPATH"] = str(root)
    # (the program only imports these modules and draws: it reads and writes no user data)
    result = subprocess.run([sys.executable, "-c", PROGRAM], capture_output=True, text=True, env=env, cwd=tmp_path, timeout=120)
    if result.returncode == NO_OPENGL:
        pytest.skip("No OpenGL 3.3 here")
    assert result.returncode == 0, result.stderr[-3000:] + result.stdout[-1000:]
    assert result.stdout.strip().endswith("ok")
