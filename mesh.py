from pathlib import Path
from time import time

import bpy
import orjson
from bpy.props import StringProperty, CollectionProperty
from bpy.types import Operator, PropertyGroup
from bpy_extras.io_utils import ImportHelper

from .blender_utils import create_empty_obj, parent_objs, try_unregister_class
from .openformats2json.gta_iv_mesh import gta_iv_mesh_to_dict
from .skinned_mesh import import_skinned_mesh


def import_mesh_handler(self, filepath: Path) -> int:
    filename = filepath.name
    file_extension = filepath.suffix

    if file_extension == ".mesh":
        mesh_data = gta_iv_mesh_to_dict(filepath.resolve())
    elif file_extension == ".json":
        with open(filepath, "r") as mesh_json:
            mesh_data = orjson.loads(mesh_json.read())
    empty = create_empty_obj(filename)
    mesh_objs = import_mesh(self, filename, empty, mesh_data, apply_skel=False)
    parent_objs(mesh_objs, empty)

    return len(mesh_objs)


def import_mesh(self, name: str, armature, data: dict, apply_skel=True, materials=None) -> list:
    if data["Version"] != "11 13":
        raise ValueError(f'Expected .mesh file version: 11 13. Got {data["Version"]}')
    mesh_objs = []
    for mesh_data in data["Geometries"]:
        mesh_objs.append(create_mesh(self, name, armature, mesh_data, data["Skinned"] and apply_skel, materials=materials))
    return mesh_objs


def create_mesh(self, name, armature, geometry_data: dict, is_skinned: bool, materials=None):
    """Creates a mesh object in Blender."""
    mesh = bpy.data.meshes.new(f"{name}")
    obj = bpy.data.objects.new(f"{name}", mesh)

    bpy.context.collection.objects.link(obj)

    faces, verts, normals, colors, uv_coords = (
        geometry_data["Indices"],
        geometry_data["Vertices"],
        geometry_data["VertxNormals"],
        geometry_data["VertxColors"],
        geometry_data["UVCoords"],
    )

    mesh.from_pydata(verts, [], faces, shade_flat=False)

    bpy.context.view_layer.objects.active = obj

    mtl_index = geometry_data["Material"]
    mesh.mtl.index = mtl_index

    # Assign material from the shader list
    if materials and 0 <= mtl_index < len(materials):
        mat = materials[mtl_index]
        obj.data.materials.append(mat)

    # Vertex colours — Blender 4.5 compatible
    # -------------------------------------------------------
    # mesh.color_attributes.new() can silently create a FloatVectorAttribute
    # instead of the requested BYTE_COLOR in Blender 4.5, making foreach_set
    # fail because FloatVectorAttributeValue has no "color" property.
    #
    # Fix: use the low-level mesh.attributes API which honours the requested
    # type, then write the data as packed RGBA bytes via foreach_set("color").
    COLOR_ATTR_NAME = "Col"

    # Remove any pre-existing attribute with this name that has the wrong type
    if COLOR_ATTR_NAME in mesh.attributes:
        existing = mesh.attributes[COLOR_ATTR_NAME]
        if existing.data_type != "BYTE_COLOR" or existing.domain != "CORNER":
            mesh.attributes.remove(existing)

    if COLOR_ATTR_NAME not in mesh.attributes:
        mesh.attributes.new(COLOR_ATTR_NAME, "BYTE_COLOR", "CORNER")

    color_attr = mesh.attributes[COLOR_ATTR_NAME]
    color_layer = color_attr.data

    # Build flat RGBA float buffer (values 0.0-1.0), one entry per loop corner
    flat_colors = []
    for loop in mesh.loops:
        flat_colors.extend([x / 255.0 for x in colors[loop.vertex_index]])
    color_layer.foreach_set("color", flat_colors)

    # Mark as the active colour for display/render
    try:
        mesh.color_attributes.active_color = mesh.color_attributes[COLOR_ATTR_NAME]
    except Exception:
        pass  # Not critical if this fails

    for i, uv_coord in enumerate(uv_coords):
        create_uv_map(mesh, f"UVMap {i}", uv_coord)

    if is_skinned:
        import_skinned_mesh(obj, armature, geometry_data)

    # normals_split_custom_set_from_vertices() was removed in Blender 4.1.
    # Custom normals are now written via a "sharp_vector" POINT attribute.
    _set_custom_normals(mesh, normals)

    if mesh.validate(verbose=True):
        self.report({"WARNING"}, "Invalid geometry corrected/removed, check console.")
    mesh.update()

    return obj


def _set_custom_normals(mesh, normals):
    """
    Set per-vertex custom normals compatible with Blender 4.1+.

    normals_split_custom_set_from_vertices() was removed in 4.1.
    The new approach writes a 'sharp_vector' float-vector attribute on the
    POINT domain; Blender reads it when computing face normals.
    """
    try:
        # Blender 4.1+ attribute-based path
        if "sharp_vector" not in mesh.attributes:
            mesh.attributes.new("sharp_vector", "FLOAT_VECTOR", "POINT")
        attr = mesh.attributes["sharp_vector"]
        flat_normals = []
        for n in normals:
            flat_normals.extend(n[:3])
        attr.data.foreach_set("vector", flat_normals)
    except Exception:
        # Fallback for any build that still exposes the legacy API
        try:
            mesh.normals_split_custom_set_from_vertices(normals)
        except AttributeError:
            pass  # Normals will be auto-computed by Blender


def create_uv_map(mesh, name, uv_coord):
    uv_layer = mesh.uv_layers.new(name=name).uv

    for loop in mesh.loops:
        u, v = uv_coord[loop.vertex_index]
        uv_layer[loop.index].vector = u, 1 - v  # flip y axis


class ImportGTAIVMesh(Operator, ImportHelper):
    """Imports meshes and lights referenced in the .odr file"""

    bl_idname = "gta4_ofio.import_mesh"
    bl_label = "Import .mesh(IV)"

    filename_ext = ".mesh"

    filter_glob: StringProperty(default="*.mesh;*.mesh.json", options={"HIDDEN"})

    files: CollectionProperty(type=PropertyGroup)

    def execute(self, context):
        folder = Path(self.filepath)
        meshes = 0
        time_start = time()
        for selection in self.files:
            fp = Path(folder.parent, selection.name)
            meshes += import_mesh_handler(self, fp)
        time_spent = time() - time_start
        self.report({"INFO"}, f"Imported {meshes} mesh(es) in {time_spent:.4f}sec")
        return {"FINISHED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


def register():
    try_unregister_class(ImportGTAIVMesh)
    bpy.utils.register_class(ImportGTAIVMesh)


def unregister():
    bpy.utils.unregister_class(ImportGTAIVMesh)
