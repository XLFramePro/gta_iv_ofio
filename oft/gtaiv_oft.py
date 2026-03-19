from pathlib import Path
from time import time

import bpy
import orjson
from bpy.props import StringProperty, CollectionProperty
from bpy.types import Operator, PropertyGroup
from bpy_extras.io_utils import ImportHelper
from loguru import logger

from ..odr.gtaiv_odr import _detect_version
from ..blender_utils import try_unregister_class
from ..blender_utils import (create_empty_obj, parent_objs, find_bone_by_id, apply_copy_transforms)
from ..light import import_lights
from ..material import create_materials
from ..mesh import import_mesh
from ..openformats2json.common import collect_meshes, collect_children
from ..openformats2json.gta_iv_light import gta_iv_light_to_dict
from ..openformats2json.gta_iv_mesh import gta_iv_mesh_to_dict
from ..openformats2json.gta_iv_skel import gta_iv_skel_to_dict
from ..openformats2json.gta_iv_wft import gta_iv_oft_to_dict
from ..openformats2json.gta_iv_wft_child import gta_iv_child_to_dict
from ..skel import import_skel


def import_oft(self, filepath: Path) -> tuple[int, int, int]:
    """
    Reads and processes the ODR file.
    """

    filename = filepath.name
    file_extension = filepath.suffix

    if file_extension == ".oft":
        oft_data =         version = _detect_version(filepath)
        if version and version != "112 2":
            raise ValueError(
                f"Unsupported .oft version: {version}\\n"
                f"File: {filepath}\\n"
                f"Expected GTA IV version 112 2."
            )
        gta_iv_oft_to_dict(filepath.resolve())
    elif file_extension == ".json":
        with open(filepath, "r") as otf_file:
            oft_data = orjson.loads(otf_file.read())
    else:
        raise NotImplementedError(f"File extension {file_extension} is not supported.")

    if oft_data.get("Version") != "112 2":
        raise ValueError(f"Version mismatch. Expected version: 110 12, got {oft_data['Version']}")
    logger.info(f"Importing OFT.json: {filename} | version: {oft_data['Version']}")

    num_bones, bones = 0, None
    if oft_data.get("Drawable").get("Skeleton") is None:
        parent_object = create_empty_obj(filename)
    else:
        skel_path = Path.joinpath(filepath.parent, oft_data["Drawable"]["Skeleton"]).resolve()
        skel_json = gta_iv_skel_to_dict(skel_path)
        parent_object, num_bones = import_skel(skel_path, filename, skel_json)
        bones = skel_json["Bones"]

    _, total_no_meshes, total_no_lights = create_drawable(self, filepath, oft_data["Drawable"], bones, parent_object=parent_object)

    children_dict = collect_children(oft_data["Fragments"]["Groups"])
    for child_path, group in children_dict.items():
        child_path = Path.joinpath(filepath.parent, child_path).resolve()
        child_data = gta_iv_child_to_dict(child_path)
        child, no_meshes, no_lights = create_drawable(self, child_path, child_data["Drawable"], bones)
        parent_objs([child], parent_object)
        apply_copy_transforms(child, parent_object, group)
        #
        total_no_meshes += no_meshes
        total_no_lights += no_lights

    bpy.context.view_layer.objects.active = parent_object
    return total_no_meshes, total_no_lights, num_bones


def create_drawable(
    self, filepath: Path, drawable_data: dict, bones: dict | None, parent_object=None
) -> tuple[bpy.types.Object, int, int]:
    filename = filepath.stem
    if parent_object is None:
        parent_object = create_empty_obj(filepath.name)
    no_meshes, no_lights = 0, 0

    # Create materials from shader definitions
    materials = []
    shader_list = drawable_data.get("Shaders", [])
    if shader_list:
        materials = create_materials(shader_list, filepath.parent)

    mesh_objs = []
    mesh_paths: list[Path] = collect_meshes(drawable_data)
    for mesh_path in mesh_paths:
        meshes = gta_iv_mesh_to_dict(Path.joinpath(filepath.parent, mesh_path).resolve())
        mesh_objs.extend(import_mesh(self, filename, parent_object, meshes, materials=materials))
    parent_object["filepath"] = str(filepath)

    light_objs = None
    if drawable_data.get("Light") is not None:
        lights = gta_iv_light_to_dict(Path.joinpath(filepath.parent, drawable_data["Light"]).resolve())
        light_objs = import_lights(filename, lights)
        lights_empty = create_empty_obj("Lights")
        parent_objs(light_objs, lights_empty)
        parent_objs([lights_empty], parent_object)
        no_lights = len(light_objs)

    if light_objs is not None and bones is not None:
        for light_obj in light_objs:
            apply_copy_transforms(
                light_obj,
                parent_object,
                find_bone_by_id(int(light_obj.data.attributes.bone_id), bones),
            )

    if mesh_objs:
        parent_objs(mesh_objs, parent_object)
        no_meshes = len(mesh_objs)

    return parent_object, no_meshes, no_lights


class ImportGTAIVOFT(Operator, ImportHelper):
    """Imports meshes and lights referenced in the .oft file"""

    bl_idname = "gta4_ofio.import_oft"
    bl_label = "Import .oft"

    filename_ext = ".oft"

    filter_glob: StringProperty(default="*.oft;*.json", options={"HIDDEN"})

    files: CollectionProperty(type=PropertyGroup)

    def execute(self, context):
        folder = Path(self.filepath)
        meshes, lights, bones = 0, 0, 0
        time_start = time()
        for selection in self.files:
            fp = Path(folder.parent, selection.name)
            x = import_oft(self, fp)
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
    try_unregister_class(ImportGTAIVOFT)
    bpy.utils.register_class(ImportGTAIVOFT)


def unregister():
    bpy.utils.unregister_class(ImportGTAIVOFT)
