from pathlib import Path
from time import time

import bpy
import orjson
from bpy.props import StringProperty, CollectionProperty
from bpy.types import Operator, PropertyGroup
from bpy_extras.io_utils import ImportHelper
from loguru import logger

from ..blender_utils import try_unregister_class
from ..blender_utils import (
    create_empty_obj,
    parent_objs,
    find_bone_by_id,
    find_bone_by_index,
    apply_copy_transforms,
)
from ..light import import_lights
from ..material import create_materials
from ..mesh import import_mesh
from ..openformats2json.common import collect_meshes
from ..openformats2json.gta_iv_light import gta_iv_light_to_dict
from ..openformats2json.gta_iv_mesh import gta_iv_mesh_to_dict
from ..openformats2json.gta_iv_skel import gta_iv_skel_to_dict
from ..openformats2json.gta_iv_wdr import gta_iv_odr_to_dict
from ..skel import import_skel


def _detect_version(filepath: Path) -> str:
    """Read the first Version line of an openFormats file."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("Version"):
                    return stripped[len("Version"):].strip()
    except OSError:
        pass
    return ""


def import_odr(self, filepath: Path) -> tuple[int, int, int]:
    filename = filepath.name
    file_extension = filepath.suffix

    # Detect version before parsing to give a clear error for non-GTA IV files
    if file_extension == ".odr":
        version = _detect_version(filepath)
        if version and version != "110 12":
            raise ValueError(
                f"Unsupported .odr version: {version}\n"
                f"File: {filepath}\n"
                f"This file is not a GTA IV .odr (expected version 110 12).\n"
                f"If this is a Max Payne 3 file, use: File > Import > Max Payne 3 openFormats > Import .odr (MP3)"
            )
        odr_data = gta_iv_odr_to_dict(filepath.resolve())
    elif file_extension == ".json":
        with open(filepath, "r") as odr_file:
            odr_data = orjson.loads(odr_file.read())
    else:
        raise NotImplementedError(f"File extension {file_extension} is not supported.")

    if odr_data.get("Version") != "110 12":
        raise ValueError(f"Version mismatch. Expected version: 110 12, got {odr_data['Version']}")
    logger.info(f"Importing ODR.json: {filename} | version: {odr_data['Version']}")

    import_result = create_drawable(self, filepath, odr_data)

    return import_result


def create_drawable(self, filepath: Path, odr_data: dict) -> tuple[int, int, int]:
    filename = filepath.stem
    num_bones, no_meshes, no_lights = 0, 0, 0
    bones = None
    if odr_data.get("Skeleton") is None:
        parent_object = create_empty_obj(filepath.name)
    else:
        skel_path = Path.joinpath(filepath.parent, odr_data["Skeleton"]).resolve()
        skel_json = gta_iv_skel_to_dict(skel_path)
        parent_object, num_bones = import_skel(skel_path, filepath.name, skel_json)
        bones = skel_json["Bones"]
    parent_object["filepath"] = str(filepath)

    # Create materials from shader definitions
    materials = []
    shader_list = odr_data.get("Shaders", [])
    if shader_list:
        materials = create_materials(shader_list, filepath.parent)

    mesh_objs = []
    mesh_paths: list[Path] = collect_meshes(odr_data)
    for index, mesh_path in enumerate(mesh_paths):
        meshes = gta_iv_mesh_to_dict(Path.joinpath(filepath.parent, mesh_path).resolve())
        _mesh_objs = import_mesh(self, mesh_path.name, parent_object, meshes, materials=materials)
        if parent_object.type == "ARMATURE" and not meshes["Skinned"]:
            bone = find_bone_by_index(index, bones)
            for mesh_obj in _mesh_objs:
                apply_copy_transforms(mesh_obj, parent_object, bone)
        mesh_objs.extend(_mesh_objs)

    light_objs = None
    if odr_data.get("Light") is not None:
        lights = gta_iv_light_to_dict(Path.joinpath(filepath.parent, odr_data["Light"]).resolve())
        light_objs = import_lights(filename, lights)
        lights_empty = create_empty_obj("Lights")
        parent_objs(light_objs, lights_empty)
        parent_objs([lights_empty], parent_object)
        no_lights = len(light_objs)

    if light_objs is not None and odr_data.get("Skeleton") is not None:
        for light_obj in light_objs:
            apply_copy_transforms(
                light_obj,
                parent_object,
                find_bone_by_id(int(light_obj.data.attributes.bone_id), bones),
            )

    if mesh_objs:
        parent_objs(mesh_objs, parent_object)

    return len(mesh_objs), no_lights, num_bones


class ImportGTAIVODR(Operator, ImportHelper):
    """Imports Open Drawable Resource"""

    bl_idname = "gta4_ofio.import_odr"
    bl_label = "Import .odr"

    filename_ext = ".odr"

    filter_glob: StringProperty(default="*.odr;*.json", options={"HIDDEN"})

    files: CollectionProperty(type=PropertyGroup)

    def execute(self, context):
        folder = Path(self.filepath)
        meshes, lights, bones = 0, 0, 0
        time_start = time()
        for selection in self.files:
            fp = Path(folder.parent, selection.name)
            x = import_odr(self, fp)
            meshes += x[0]
            lights += x[1]
            bones += x[2]
        time_spent = time() - time_start
        self.report(
            {"INFO"},
            f"Imported {meshes} meshes, {lights} lights, and {bones} bones in {time_spent:.4f}sec",
        )
        return {"FINISHED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


def register():
    try_unregister_class(ImportGTAIVODR)
    bpy.utils.register_class(ImportGTAIVODR)


def unregister():
    bpy.utils.unregister_class(ImportGTAIVODR)
