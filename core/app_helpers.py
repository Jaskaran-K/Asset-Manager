"""Importing a model into a Maya or Blender that's already open.

A program started from outside always opens a new window, so a window that's open has to be asked
from inside: a small helper script, installed once from Settings > Apps, starts with the app. It
writes a file saying it's there, and checks a folder for requests, each naming a model to import.
There's no network port, and a request can only ever import a model of the types listed: it runs no
commands. Maya has one helper for all its versions (its shared scripts folder); Blender one for each
version (its scripts/startup folder, which it runs without any preference being changed).

Only OBJ, FBX and glTF go this way. Scenes (.ma, .mb, .blend) still open in a new window, since
opening one in a window that's in use would replace what's there.

A request can also be a texture set (core.texture_sets) to make into a material: Blender gets a
Principled BSDF, Maya an Arnold aiStandardSurface (or a standardSurface: Settings > Apps), put on the
selected objects. It names image files and what each is; the helper checks they're images that exist.

Helpers are versioned (HELPER_VERSION): one that's installed is brought up to date by refresh(), and a
window says which version it runs in its "alive" file, so a material isn't sent to one that's too old
(it needs restarting once).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QStandardPaths

from . import video_tools

HELPER_FILE = "asset_manager_helper.py"
HELPER_VERSION = 2  # 2: materials
MATERIAL_VERSION = 2  # the first version that makes materials
IMAGE_TYPES = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".tga", ".bmp", ".exr", ".hdr", ".psd", ".webp")
MATERIAL_ROLES = ("color", "normal", "roughness", "gloss", "metal", "ao", "height", "opacity", "emissive", "specular", "packed")
REQUEST_SUFFIX = ".request"
ALIVE_PREFIX = "alive-"
MAX_REQUEST_SECONDS = 120  # older ones are dropped: a window opened later doesn't import what's long forgotten

IMPORT_TYPES: dict[str, tuple[str, ...]] = {
    "maya": (".obj", ".fbx"),
    "blender": (".obj", ".fbx", ".glb", ".gltf"),
}
_PROGRAMS = {"maya": ("maya.exe",), "blender": ("blender.exe",)}  # (the running window, not Blender's launcher)
_BLOCK_START = "# Asset Manager's helper (Settings > Apps adds and removes this)"
_BLOCK_END = "# End of Asset Manager's helper"
_USER_SETUP_BLOCK = f"""{_BLOCK_START}
try:
    import asset_manager_helper
    asset_manager_helper.start()
except Exception as exc:
    print("Asset Manager's helper didn't start:", exc)
{_BLOCK_END}
"""

_COMMON = '''"""Asset Manager's helper: imports a model into this window when the Asset Manager asks.

Added by its Settings > Apps, which also removes it. It only reads the folder below, and only ever
imports model files. Safe to delete.
"""

import json
import os
import time

FOLDER = @FOLDER@
TYPES = @TYPES@
MAX_AGE = @MAX_AGE@
VERSION = @VERSION@
IMAGE_TYPES = @IMAGE_TYPES@
ROLES = @ROLES@


def announce():
    """Say this window is here, and which version of the helper it runs (the Asset Manager checks that
    its process is still running)."""
    os.makedirs(FOLDER, exist_ok=True)
    with open(os.path.join(FOLDER, "@ALIVE@%d" % os.getpid()), "w") as f:
        f.write(str(VERSION))


def checked_material(spec):
    """A material request as it may be used: image files that exist, of known kinds; None if it isn't one."""
    if not isinstance(spec, dict) or not isinstance(spec.get("name"), str) or not isinstance(spec.get("maps"), list):
        return None
    maps = []
    for entry in spec["maps"]:
        if not isinstance(entry, dict):
            return None
        path, role = entry.get("file"), entry.get("role")
        if not isinstance(path, str) or role not in ROLES or not os.path.isfile(path):
            return None
        if os.path.splitext(path)[1].lower() not in IMAGE_TYPES:
            return None
        channels = entry.get("channels")
        if channels is not None and (not isinstance(channels, list) or len(channels) != 3
                                     or any(c not in ("ao", "roughness", "metal") for c in channels)):
            return None
        maps.append({"file": path, "role": role, "channels": channels, "tiled": bool(entry.get("tiled"))})
    if not maps:
        return None
    return {"name": spec["name"][:60] or "Material", "maps": maps, "flip_green": bool(spec.get("flip_green")),
            "arnold": spec.get("shader") != "standard"}


def has_requests():
    try:
        return any(name.endswith("@SUFFIX@") for name in os.listdir(FOLDER))
    except OSError:
        return False


def check():
    """Import what's been asked for. Renaming a request first means only one open window gets it."""
    try:
        names = sorted(os.listdir(FOLDER))
    except OSError:
        return
    for name in names:
        if not name.endswith("@SUFFIX@"):
            continue
        request = os.path.join(FOLDER, name)
        claimed = "%s.%d" % (request, os.getpid())
        try:
            os.replace(request, claimed)
        except OSError:
            continue
        try:
            age = time.time() - os.path.getmtime(claimed)
            with open(claimed, encoding="utf-8") as f:
                request = json.load(f)
            path = request.get("file")
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            request, path = None, None
        finally:
            try:
                os.remove(claimed)
            except OSError:
                pass
        if isinstance(request, dict) and "material" in request and age <= MAX_AGE:
            material = checked_material(request["material"])
            if material is not None:
                try:
                    make_material(material)
                except Exception as exc:
                    print("Asset Manager's helper couldn't make the material %s: %s" % (material["name"], exc))
            continue
        if not isinstance(path, str) or age > MAX_AGE or not os.path.isfile(path):
            continue
        extension = os.path.splitext(path)[1].lower()
        if extension in TYPES:
            try:
                import_model(path, extension)
            except Exception as exc:
                print("Asset Manager's helper couldn't import %s: %s" % (path, exc))
'''

_MAYA = '''

import threading

_waiting = False


def import_model(path, extension):
    from maya import cmds

    plugin, kind = {".obj": ("objExport", "OBJ"), ".fbx": ("fbxmaya", "FBX")}[extension]
    cmds.loadPlugin(plugin, quiet=True)
    cmds.file(path, i=True, type=kind, ignoreVersion=True)


def _maya_file(name, path, data, tiled):
    """A file texture node (and its placement), Raw for data maps; tiled: UDIM tiles."""
    from maya import cmds

    node = cmds.shadingNode("file", asTexture=True, isColorManaged=True, name=name)
    place = cmds.shadingNode("place2dTexture", asUtility=True, name=name + "_place")
    for attribute in ("coverage", "translateFrame", "rotateFrame", "mirrorU", "mirrorV", "stagger", "wrapU", "wrapV",
                      "repeatUV", "offset", "rotateUV", "noiseUV", "vertexUvOne", "vertexUvTwo", "vertexUvThree",
                      "vertexCameraOne"):
        cmds.connectAttr(place + "." + attribute, node + "." + attribute, force=True)
    cmds.connectAttr(place + ".outUV", node + ".uv", force=True)
    cmds.connectAttr(place + ".outUvFilterSize", node + ".uvFilterSize", force=True)
    cmds.setAttr(node + ".fileTextureName", path, type="string")
    if tiled:
        cmds.setAttr(node + ".uvTilingMode", 3)  # (UDIM, Mari)
    if data:
        cmds.setAttr(node + ".colorSpace", "Raw", type="string")
        cmds.setAttr(node + ".ignoreColorSpaceFileRules", 1)
        cmds.setAttr(node + ".alphaIsLuminance", 1)
    return node


def make_material(spec):
    """An aiStandardSurface (or standardSurface) with the maps, on the selected objects; one undo step."""
    from maya import cmds

    arnold = spec["arnold"]
    if arnold:
        try:
            cmds.loadPlugin("mtoa", quiet=True)
        except Exception:
            arnold = False
        if arnold and "aiStandardSurface" not in (cmds.listNodeTypes("shader") or []):
            arnold = False
        if not arnold:
            print("Asset Manager's helper: Arnold isn't loaded, so %s is a standardSurface." % spec["name"])
    selected = cmds.ls(selection=True, long=True) or []
    cmds.undoInfo(openChunk=True, chunkName="Asset Manager material")
    try:
        base = "".join(c if c.isalnum() else "_" for c in spec["name"]) or "Material"
        if base[0].isdigit():
            base = "M_" + base
        shader = cmds.shadingNode("aiStandardSurface" if arnold else "standardSurface", asShader=True, name=base)
        group = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=shader + "SG")
        cmds.connectAttr(shader + ".outColor", group + ".surfaceShader", force=True)
        for entry in spec["maps"]:
            role, path = entry["role"], entry["file"]
            node = _maya_file("%s_%s_tex" % (shader, role), path, role != "color" and role != "emissive", entry["tiled"])
            if role == "color":
                cmds.connectAttr(node + ".outColor", shader + ".baseColor", force=True)
            elif role == "roughness":
                cmds.connectAttr(node + ".outAlpha", shader + ".specularRoughness", force=True)
            elif role == "gloss":
                flip = cmds.shadingNode("reverse", asUtility=True, name=shader + "_gloss_to_roughness")
                cmds.connectAttr(node + ".outAlpha", flip + ".inputX", force=True)
                cmds.connectAttr(flip + ".outputX", shader + ".specularRoughness", force=True)
            elif role == "metal":
                cmds.connectAttr(node + ".outAlpha", shader + ".metalness", force=True)
            elif role == "normal":
                cmds.setAttr(node + ".alphaIsLuminance", 0)
                if arnold:
                    normal = cmds.shadingNode("aiNormalMap", asUtility=True, name=shader + "_normal")
                    cmds.connectAttr(node + ".outColor", normal + ".input", force=True)
                    cmds.setAttr(normal + ".invertY", 1 if spec["flip_green"] else 0)
                    cmds.connectAttr(normal + ".outValue", shader + ".normalCamera", force=True)
                else:
                    bump = cmds.shadingNode("bump2d", asUtility=True, name=shader + "_normal")
                    cmds.setAttr(bump + ".bumpInterp", 1)  # (tangent space normals)
                    cmds.connectAttr(node + ".outAlpha", bump + ".bumpValue", force=True)
                    cmds.connectAttr(bump + ".outNormal", shader + ".normalCamera", force=True)
            elif role == "height":
                displacement = cmds.shadingNode("displacementShader", asShader=True, name=shader + "_displacement")
                cmds.connectAttr(node + ".outAlpha", displacement + ".displacement", force=True)
                cmds.setAttr(displacement + ".scale", 0.1)
                cmds.connectAttr(displacement + ".displacement", group + ".displacementShader", force=True)
            elif role == "emissive":
                cmds.connectAttr(node + ".outColor", shader + ".emissionColor", force=True)
                cmds.setAttr(shader + ".emission", 1)
            elif role == "opacity":
                cmds.setAttr(node + ".alphaIsLuminance", 0)
                cmds.connectAttr(node + ".outColor", shader + ".opacity", force=True)
            elif role == "specular":
                cmds.connectAttr(node + ".outAlpha", shader + ".specular", force=True)
            elif role == "packed":
                cmds.setAttr(node + ".alphaIsLuminance", 0)
                for channel, use in zip("RGB", entry["channels"] or ("ao", "roughness", "metal")):
                    target = {"roughness": "specularRoughness", "metal": "metalness"}.get(use)
                    if target:
                        cmds.connectAttr(node + ".outColor" + channel, shader + "." + target, force=True)
            # (ao: the file is there, unconnected: a renderer makes its own shadowing)
        meshes = [o for o in selected if cmds.listRelatives(o, shapes=True) or cmds.objectType(o) == "mesh"]
        if meshes:
            cmds.sets(meshes, edit=True, forceElement=group)
        cmds.select(shader, replace=True) if not meshes else cmds.select(selected, replace=True)
        print("Asset Manager: made %s%s" % (shader, " on %d objects" % len(meshes) if meshes else ""))
    finally:
        cmds.undoInfo(closeChunk=True)
    return shader


def _run_waiting():
    global _waiting
    _waiting = False
    check()


def _watch():
    """Only looks at the folder here: Maya's own thread is asked to act when there's something to do."""
    global _waiting
    import maya.utils

    while True:
        time.sleep(1)
        if not _waiting and has_requests():
            _waiting = True
            maya.utils.executeDeferred(_run_waiting)


def start():
    from maya import cmds

    if cmds.about(batch=True):
        return  # (a batch job or a render: not a window to import into)
    announce()
    threading.Thread(target=_watch, name="asset-manager-helper", daemon=True).start()
'''

_BLENDER = '''

import bpy


def import_model(path, extension):
    operator = {
        ".obj": bpy.ops.wm.obj_import, ".fbx": bpy.ops.import_scene.fbx,
        ".glb": bpy.ops.import_scene.gltf, ".gltf": bpy.ops.import_scene.gltf,
    }[extension]
    windows = bpy.context.window_manager.windows
    if windows:  # (a timer has no window of its own, and the importers want one)
        with bpy.context.temp_override(window=windows[0]):
            operator("EXEC_DEFAULT", True, filepath=path)  # (True: Ctrl+Z undoes it)
    else:
        operator("EXEC_DEFAULT", True, filepath=path)


LABELS = {"color": "Base Color", "ao": "Ambient Occlusion (not connected)", "metal": "Metallic", "packed": "Packed (ORM)"}


def _image(path, data, tiled):
    image = bpy.data.images.load(path, check_existing=True)
    if tiled:
        image.source = "TILED"
    if data:
        try:
            image.colorspace_settings.name = "Non-Color"
        except TypeError:
            image.colorspace_settings.is_data = True
    return image


def _socket(node, *names):
    for name in names:
        if name in node.inputs:
            return node.inputs[name]
    return None


def make_material(spec):
    """A Principled BSDF material with the maps, on the selected objects; one undo step."""
    material = bpy.data.materials.new(spec["name"])
    material.use_nodes = True
    nodes, links = material.node_tree.nodes, material.node_tree.links
    shader = next(n for n in nodes if n.type == "BSDF_PRINCIPLED")
    output = next(n for n in nodes if n.type == "OUTPUT_MATERIAL")
    shader.location, output.location = (300, 300), (650, 300)
    row = 0

    def texture(entry, data):
        nonlocal row
        node = nodes.new("ShaderNodeTexImage")
        node.image = _image(entry["file"], data, entry["tiled"])
        node.location = (-500, 600 - row * 300)
        node.label = LABELS.get(entry["role"], entry["role"].title())
        row += 1
        return node

    def separate(node):
        try:
            split = nodes.new("ShaderNodeSeparateColor")
            names = ("Red", "Green", "Blue")
        except RuntimeError:
            split = nodes.new("ShaderNodeSeparateRGB")
            names = ("R", "G", "B")
        split.location = (node.location.x + 300, node.location.y)
        links.new(node.outputs["Color"], split.inputs[0])
        return split, names

    for entry in spec["maps"]:
        role = entry["role"]
        node = texture(entry, role not in ("color", "emissive"))
        if role == "color":
            links.new(node.outputs["Color"], _socket(shader, "Base Color"))
        elif role == "roughness":
            links.new(node.outputs["Color"], _socket(shader, "Roughness"))
        elif role == "gloss":
            flip = nodes.new("ShaderNodeInvert")
            flip.location = (node.location.x + 300, node.location.y)
            links.new(node.outputs["Color"], flip.inputs["Color"])
            links.new(flip.outputs["Color"], _socket(shader, "Roughness"))
        elif role == "metal":
            links.new(node.outputs["Color"], _socket(shader, "Metallic"))
        elif role == "normal":
            normal = nodes.new("ShaderNodeNormalMap")
            normal.location = (node.location.x + 550, node.location.y)
            colour = node.outputs["Color"]
            if spec["flip_green"]:  # (DirectX: green the other way up)
                split, names = separate(node)
                try:
                    join = nodes.new("ShaderNodeCombineColor")
                except RuntimeError:
                    join = nodes.new("ShaderNodeCombineRGB")
                flip = nodes.new("ShaderNodeMath")
                flip.operation = "SUBTRACT"
                flip.inputs[0].default_value = 1.0
                links.new(split.outputs[names[1]], flip.inputs[1])
                links.new(split.outputs[names[0]], join.inputs[0])
                links.new(flip.outputs[0], join.inputs[1])
                links.new(split.outputs[names[2]], join.inputs[2])
                colour = join.outputs[0]
            links.new(colour, normal.inputs["Color"])
            links.new(normal.outputs["Normal"], _socket(shader, "Normal"))
        elif role == "height":
            displacement = nodes.new("ShaderNodeDisplacement")
            displacement.location = (node.location.x + 550, node.location.y)
            displacement.inputs["Scale"].default_value = 0.1
            links.new(node.outputs["Color"], displacement.inputs["Height"])
            links.new(displacement.outputs["Displacement"], output.inputs["Displacement"])
        elif role == "emissive":
            links.new(node.outputs["Color"], _socket(shader, "Emission Color", "Emission"))
            strength = _socket(shader, "Emission Strength")
            if strength is not None:
                strength.default_value = 1.0
        elif role == "opacity":
            links.new(node.outputs["Color"], _socket(shader, "Alpha"))
            if hasattr(material, "blend_method"):
                material.blend_method = "HASHED"
        elif role == "specular":
            socket = _socket(shader, "Specular IOR Level", "Specular")
            if socket is not None:
                links.new(node.outputs["Color"], socket)
        elif role == "packed":
            split, names = separate(node)
            for name, use in zip(names, entry["channels"] or ("ao", "roughness", "metal")):
                socket = {"roughness": _socket(shader, "Roughness"), "metal": _socket(shader, "Metallic")}.get(use)
                if socket is not None:
                    links.new(split.outputs[name], socket)
        # (ao: the image is there, unconnected: Cycles and Eevee make their own shadowing)
    objects = [o for o in bpy.context.selected_objects if o.type in ("MESH", "CURVE", "SURFACE", "META", "FONT")]
    for obj in objects:
        if obj.data.materials:
            obj.data.materials[obj.active_material_index] = material
        else:
            obj.data.materials.append(material)
    windows = bpy.context.window_manager.windows
    if windows:
        with bpy.context.temp_override(window=windows[0]):
            bpy.ops.ed.undo_push(message="Asset Manager material")
    print("Asset Manager: made %s%s" % (material.name, " on %d objects" % len(objects) if objects else ""))
    return material


def _tick():
    check()
    return 1.0


def register():
    if bpy.app.background:
        return  # (a batch job or a render: not a window to import into)
    announce()
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=1.0, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
'''


class HelperError(Exception):
    """A helper couldn't be added or removed (the message says why, for the user)."""


def mailbox(app_id: str) -> Path:
    """Where the helper of an app looks for requests."""
    return video_tools.CACHE_ROOT / "app-requests" / app_id


def helper_source(app_id: str) -> str:
    """The helper script for an app."""
    source = _COMMON + (_MAYA if app_id == "maya" else _BLENDER)
    for mark, value in (
        ("@FOLDER@", repr(str(mailbox(app_id)))), ("@TYPES@", repr(IMPORT_TYPES[app_id])),
        ("@MAX_AGE@", str(MAX_REQUEST_SECONDS)), ("@ALIVE@", ALIVE_PREFIX), ("@SUFFIX@", REQUEST_SUFFIX),
        ("@VERSION@", str(HELPER_VERSION)), ("@IMAGE_TYPES@", repr(IMAGE_TYPES)), ("@ROLES@", repr(MATERIAL_ROLES)),
    ):
        source = source.replace(mark, value)
    return source


# ----------------------------------------------------------------------
# Asking an open window
# ----------------------------------------------------------------------
def _process_program(pid: int) -> Optional[str]:
    """The program a running process was started from (None: there's no such process)."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1000, False, pid)  # (PROCESS_QUERY_LIMITED_INFORMATION)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(1024)
        name = ctypes.create_unicode_buffer(size.value)
        return name.value if kernel32.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)) else None
    finally:
        kernel32.CloseHandle(handle)


def window_is_open(app_id: str) -> bool:
    """Whether a window of the app with its helper is running. A process id can be reused, so its program
    is checked too; a window that's gone leaves a file, removed here."""
    return open_window_version(app_id) is not None


def open_window_version(app_id: str) -> Optional[int]:
    """The newest helper version running in an open window of the app (1: from before versions), or None
    if none is open."""
    if app_id not in IMPORT_TYPES:
        return None
    folder = mailbox(app_id)
    try:
        names = os.listdir(folder)
    except OSError:
        return None
    newest: Optional[int] = None
    for name in names:
        if not name.startswith(ALIVE_PREFIX):
            continue
        program = _process_program(int(name[len(ALIVE_PREFIX):])) if name[len(ALIVE_PREFIX):].isdigit() else None
        if program is not None and os.path.basename(program).lower() in _PROGRAMS[app_id]:
            try:
                text = (folder / name).read_text(encoding="utf-8").strip()
            except OSError:
                text = ""
            version = int(text) if text.isdigit() else 1
            newest = version if newest is None else max(newest, version)
        else:
            try:
                os.remove(folder / name)
            except OSError:
                pass
    return newest


def goes_to_open_window(app_id: str, file: Path | str) -> bool:
    """Whether a file would be imported into an open window, rather than start a new one."""
    return os.path.splitext(str(file))[1].lower() in IMPORT_TYPES.get(app_id, ()) and window_is_open(app_id)


def send(app_id: str, file: Path | str) -> None:
    """Ask the open window to import a file (raises OSError). It's picked up within a second or so, or
    as soon as the app is free. Written whole, then given its name, so it's never read half written."""
    folder = mailbox(app_id)
    folder.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for entry in os.scandir(folder):
        if entry.name.endswith(REQUEST_SUFFIX) and now - entry.stat().st_mtime > MAX_REQUEST_SECONDS:
            try:
                os.remove(entry.path)
            except OSError:
                pass
    _post(folder, {"file": str(file)})


def _post(folder: Path, request: dict) -> None:
    name = uuid.uuid4().hex
    partial = folder / f"{name}.partial"
    partial.write_text(json.dumps(request), encoding="utf-8")
    os.replace(partial, folder / f"{name}{REQUEST_SUFFIX}")


def send_material(app_id: str, material: dict) -> None:
    """Ask the open window to make a material (raises OSError): material is {"name", "maps": [{"file",
    "role", "channels", "tiled"}], "flip_green", "shader": "arnold" / "standard"} (see material_request)."""
    folder = mailbox(app_id)
    folder.mkdir(parents=True, exist_ok=True)
    _post(folder, {"material": material})


def material_request(texture_set, files: dict, shader: str = "arnold", choices: Optional[dict] = None,
                     flip_green: Optional[bool] = None) -> dict:
    """The request for a texture set (core.texture_sets.TextureSet). files: {str(asset.path): the real file}
    (a file inside an archive unpacked); choices: the map used for each kind ({role: TextureMap}: as the
    material window has them), else the set's own; a UDIM set names its first tile, marked tiled."""
    if choices is None:
        choices = {role: chosen[0] for role in MATERIAL_ROLES if (chosen := texture_set.choice(role))}
    maps = []
    for role in MATERIAL_ROLES:
        chosen = choices.get(role)
        real = files.get(str(chosen.asset.path)) if chosen is not None else None
        if real is None:
            continue
        maps.append({
            "file": str(real), "role": role, "tiled": chosen.tile is not None,
            "channels": list(chosen.channels) if role == "packed" else None,
        })
    flip = texture_set.flip_green() if flip_green is None else flip_green
    return {"name": texture_set.name, "maps": maps, "flip_green": bool(flip), "shader": shader}


# ----------------------------------------------------------------------
# Adding and removing the helpers
# ----------------------------------------------------------------------
def _maya_scripts() -> Path:
    """Maya's scripts folder for all its versions."""
    root = os.environ.get("MAYA_APP_DIR")
    if root:
        return Path(root) / "scripts"
    return Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)) / "maya" / "scripts"


def _blender_startup(program: Optional[str]) -> Path:
    """The folder of scripts that the Blender at this path runs when it starts."""
    root = os.environ.get("BLENDER_USER_SCRIPTS")
    if root:
        return Path(root) / "startup"
    match = re.search(r"(\d+\.\d+)", os.path.basename(os.path.dirname(program or "")))
    if match is None or not os.environ.get("APPDATA"):
        raise HelperError("Couldn't tell which version of Blender this is, from the name of its folder.")
    return Path(os.environ["APPDATA"]) / "Blender Foundation" / "Blender" / match.group(1) / "scripts" / "startup"


def _helper_path(app_id: str, program: Optional[str]) -> Path:
    return (_maya_scripts() if app_id == "maya" else _blender_startup(program)) / HELPER_FILE


def is_installed(app_id: str, program: Optional[str]) -> bool:
    try:
        return _helper_path(app_id, program).is_file()
    except HelperError:
        return False


def is_current(app_id: str, program: Optional[str]) -> bool:
    """Whether the installed helper is this version of the app's."""
    try:
        return _helper_path(app_id, program).read_text(encoding="utf-8") == helper_source(app_id)
    except (OSError, UnicodeError, HelperError):
        return False


def refresh(app_id: str, program: Optional[str]) -> bool:
    """Bring an installed helper up to date (the window runs it once it's started again). True if it was
    rewritten."""
    if not is_installed(app_id, program) or is_current(app_id, program):
        return False
    try:
        install(app_id, program)
    except HelperError:
        return False
    return True


def install(app_id: str, program: Optional[str]) -> None:
    """Add the helper (raises HelperError). The app runs it the next time it starts."""
    try:
        path = _helper_path(app_id, program)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(helper_source(app_id), encoding="utf-8")
        if app_id == "maya":
            _add_to_user_setup(path.parent / "userSetup.py")
    except (OSError, UnicodeError) as exc:
        raise HelperError(f"The helper couldn't be added: {exc}") from exc


def remove(app_id: str, program: Optional[str]) -> None:
    """Remove the helper (raises HelperError). A window running it keeps it until it's closed."""
    try:
        path = _helper_path(app_id, program)
        if app_id == "maya":
            _remove_from_user_setup(path.parent / "userSetup.py")
        path.unlink(missing_ok=True)
    except (OSError, UnicodeError) as exc:
        raise HelperError(f"The helper couldn't be removed: {exc}") from exc


def _add_to_user_setup(path: Path) -> None:
    """Add our lines to Maya's startup script. Whatever's there is left as it is."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if _BLOCK_START in text:
        return
    if text and not text.endswith("\n"):
        text += "\n"
    _write_whole(path, text + _USER_SETUP_BLOCK)


def _remove_from_user_setup(path: Path) -> None:
    """Take out only our lines (a script that held nothing else goes too)."""
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    start, end = text.find(_BLOCK_START), text.find(_BLOCK_END)
    if start < 0 or end < start:
        return
    end += len(_BLOCK_END)
    if text[end:end + 1] == "\n":
        end += 1
    rest = text[:start] + text[end:]
    if rest.strip():
        _write_whole(path, rest)
    else:
        path.unlink()


def _write_whole(path: Path, text: str) -> None:
    partial = path.with_name(path.name + ".partial")
    partial.write_text(text, encoding="utf-8")
    os.replace(partial, path)
