from pathlib import Path
from time import time
from typing import Optional

import bpy
import orjson
from bpy.props import StringProperty, CollectionProperty
from bpy.types import Operator, PropertyGroup
from bpy_extras.io_utils import ImportHelper
from loguru import logger

from ..odr.gtaiv_odr import _detect_version
from ..blender_utils import try_unregister_class
from ..blender_utils import create_empty_obj, parent_objs, apply_copy_transforms, find_bone_by_id
from ..light import import_lights
from ..material import create_materials
from ..mesh import import_mesh
from ..openformats2json.common import collect_meshes
from ..openformats2json.gta_iv_light import gta_iv_light_to_dict
from ..openformats2json.gta_iv_mesh import gta_iv_mesh_to_dict
from ..openformats2json.gta_iv_skel import gta_iv_skel_to_dict
from ..openformats2json.gta_iv_wdd import gta_iv_odd_to_dict
from ..skel import import_skel


def import_odd(self, filepath: Path) -> tuple[int, int, int]:
    filename = filepath.name
    file_extension = filepath.suffix

    if file_extension == ".odd":
        odd_data =         version = _detect_version(filepath)
        if version and version != "110 12":
            raise ValueError(
                f"Unsupported .odd version: {version}\\n"
                f"File: {filepath}\\n"
                f"Expected GTA IV version 110 12."
            )
        gta_iv_odd_to_dict(filepath.resolve())
    elif file_extension == ".json":
        with open(filepath, "r") as odd_file:
            odd_data = orjson.loads(odd_file.read())
    else:
        raise NotImplementedError(f"File extension {file_extension} is not supported.")

    if odd_data.get("Version") != "110 12":
        raise ValueError(f"Version mismatch. Expected version: 110 12, got {odd_data['Version']}")

    logger.info(f"Importing odd.json: {filename} | version: {odd_data['Version']}")
    import_result = create_drawable_directory(self, filepath, odd_data)

    return import_result


def create_drawable_directory(self, filepath: Path, odd_data: dict) -> tuple[int, int, int]:
    num_bones, no_meshes, no_lights = 0, 0, 0
    bones = None
    skel = get_skel(odd_data)
    if skel is None:
        parent_object = create_empty_obj(filepath.name)
    else:
        skel_path = Path.joinpath(filepath.parent, skel).resolve()
        skel_json = gta_iv_skel_to_dict(skel_path)
        parent_object, num_bones = import_skel(skel_path, filepath.name, skel_json)
        bones = skel_json["Bones"]
    parent_object["filepath"] = str(filepath)

    odd_objs = []
    for drawable in odd_data["Drawables"]:
        odd_obj = create_empty_obj(drawable)
        drawable_data = odd_data["Drawables"][drawable]

        # Create materials from shader definitions
        materials = []
        shader_list = drawable_data.get("Shaders", [])
        if shader_list:
            materials = create_materials(shader_list, filepath.parent)

        mesh_objs = None
        mesh_paths: list[Path] = collect_meshes(drawable_data)
        for mesh_path in mesh_paths:
            meshes = gta_iv_mesh_to_dict(Path.joinpath(filepath.parent, mesh_path).resolve())
            mesh_objs = import_mesh(self, drawable, parent_object, meshes, materials=materials)
            no_meshes += len(mesh_objs)

        light_objs = None
        if odd_data["Drawables"][drawable].get("Light") is not None:
            lights = gta_iv_light_to_dict(
                Path.joinpath(filepath.parent, odd_data["Drawables"][drawable]["Light"]).resolve()
            )
            light_objs = import_lights(drawable, lights)
            lights_empty = create_empty_obj("Lights")
            parent_objs(light_objs, lights_empty)
            parent_objs([lights_empty], odd_obj)
            no_lights = len(light_objs)

        if light_objs is not None and skel is not None:
            for light_obj in light_objs:
                apply_copy_transforms(
                    light_obj,
                    parent_object,
                    find_bone_by_id(int(light_obj.data.attributes.bone_id), bones),
                )

        if mesh_objs is not None:
            parent_objs(mesh_objs, odd_obj)
        odd_objs.append(odd_obj)

    parent_objs(odd_objs, parent_object)

    return no_meshes, no_lights, num_bones


def get_skel(data: dict) -> Optional[Path]:
    for drawable in data["Drawables"].values():
        if drawable.get("Skeleton") is not None:
            return drawable["Skeleton"]
    return None


class ImportGTAIVODD(Operator, ImportHelper):
    """Imports Open Drawable Dictionary"""

    bl_idname = "gta4_ofio.import_odd"
    bl_label = "Import .odd(IV)"

    filename_ext = ".odd"

    filter_glob: StringProperty(default="*.odd;*.odd.json", options={"HIDDEN"})

    files: CollectionProperty(type=PropertyGroup)

    def execute(self, context):
        folder = Path(self.filepath).parent
        meshes, lights, bones = 0, 0, 0
        time_start = time()
        for selection in self.files:
            fp = Path(folder, selection.name)
            x = import_odd(self, fp)
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
    try_unregister_class(ImportGTAIVODD)
    bpy.utils.register_class(ImportGTAIVODD)


def unregister():
    bpy.utils.unregister_class(ImportGTAIVODD)
