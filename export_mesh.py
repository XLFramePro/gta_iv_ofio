from __future__ import annotations

from os.path import dirname, join as join_path

import bpy
from bpy.props import BoolProperty, StringProperty
from bpy.types import Operator
from bpy_extras.io_utils import ExportHelper
from mathutils import Vector
from bpy.path import basename

from .blender_utils import try_unregister_class
from .openformats2json.to_openFormats.gta_iv_mesh import gta_iv_dict_to_mesh

# ? https://stackoverflow.com/a/79043194


def _read_color_layer_flat(color_layer, count):
    """Read all RGBA values from a color attribute data block into a flat list.
    Uses foreach_get which works on all Blender 4.x attribute subtypes."""
    buf = [0.0] * (count * 4)
    color_layer.foreach_get("color", buf)
    return buf


def _get_color_at(color_layer_flat, index):
    """Return [R,G,B,A] as 0-255 ints from the pre-fetched flat buffer."""
    base = index * 4
    return [int(color_layer_flat[base + i] * 255) for i in range(4)]


def get_bone_by_name(bone_name, armature):
    for bone in armature.data.bones:
        if bone.name == bone_name:
            return bone
    return None


def export(self, data, obj, *, calc_tangents=False, keep_tmp_mesh=False):
    mesh_data = export_mesh(self, obj, data["Skinned"], calc_tangents=calc_tangents, keep_tmp_mesh=keep_tmp_mesh)
    if mesh_data is None:
        return False
    mesh_data["Material"] = obj.data.mtl.index
    data["Geometries"].append(mesh_data)
    return True


def prepare_mesh(mesh):
    v_uv_map = dict()

    for face in mesh.polygons:
        face.select = True

    bpy.ops.object.mode_set(mode="EDIT")

    bpy.ops.mesh.quads_convert_to_tris()

    bpy.ops.mesh.select_all(action='DESELECT')
    bpy.ops.mesh.select_mode(use_extend=False, use_expand=False, type='EDGE')

    bpy.ops.object.mode_set(mode="OBJECT")

    edges = mesh.edges
    uv_layer = mesh.uv_layers[0]

    for l in mesh.loops:
        uv = uv_layer.uv[l.index].vector.to_tuple(6)
        if l.vertex_index not in v_uv_map:
            v_uv_map[l.vertex_index] = uv
        elif v_uv_map[l.vertex_index] != uv:
            edges[l.edge_index].select = True

    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.edge_split(type='VERT')
    bpy.ops.object.mode_set(mode="OBJECT")
    return mesh

def export_mesh(self, mesh_obj, is_skinned: bool, num_uvs: int = 6, num_uvs_skinned: int = 2, *, calc_tangents=False, keep_tmp_mesh=False):
    num_uvs_required = num_uvs_skinned if is_skinned else num_uvs
    mesh_obj.select_set(True)
    bpy.context.view_layer.objects.active = mesh_obj
    bpy.ops.object.duplicate(linked = False)
    _mesh_obj = bpy.context.object
    mesh = prepare_mesh(_mesh_obj.data)
    armature = mesh_obj.find_armature()
    vertex_groups = _mesh_obj.vertex_groups
    fill_index = 0
    if vertex_groups:
        if armature is None:
            # TODO:
            self.report({"ERROR"}, "Mesh is weight painted without an armature")
            return None
        bone = get_bone_by_name(vertex_groups[0].name, armature)
        if bone is not None:
            fill_index = bone.attr.index


    vertex_count = len(mesh.vertices)
    idx, vertices, vertex_normals, vertex_colors = (
        [], [], [(0.0, 0.0, 0.0) for _ in range(vertex_count)], [],
    )
    bone_indices, bone_weights, tangents = [], [], [(0.0, 0.0, 0.0, 0.0) for _ in range(vertex_count)]

    has_vertex_color = mesh.color_attributes.active_color is not None
    color_layer_type = mesh.color_attributes.active_color.domain if has_vertex_color else None
    color_layer = mesh.color_attributes.active_color.data if has_vertex_color else None
    # Pre-fetch all color data into a flat buffer to avoid per-element .color
    # access which breaks on FloatVectorAttributeValue in Blender 4.x.
    if has_vertex_color and color_layer is not None:
        _color_count = len(color_layer)
        _color_buf = _read_color_layer_flat(color_layer, _color_count)
    else:
        _color_buf = None

    uv_layers = mesh.uv_layers[:num_uvs_required]
    _tmp = [(0.0, 0.0) for _ in range(vertex_count)]
    uv_layer_count = len(uv_layers)
    uv_layers_data = [_tmp.copy() for _ in range(uv_layer_count)]
    uv_layers_data.extend([None for _ in range(num_uvs_required - uv_layer_count)])

    mesh.calc_loop_triangles()

    triangles = mesh.loop_triangles

    # Process triangles & UVs
    for loop_tri in triangles:
        triangle_indices = []
        for loop_index in loop_tri.loops:
            vert_index = mesh.loops[loop_index].vertex_index
            triangle_indices.append(vert_index)
            vertex_normals[vert_index] = mesh.loops[loop_index].normal.to_tuple(7)
            if color_layer_type == "CORNER":
                color = _get_color_at(_color_buf, loop_index) if _color_buf else [255, 255, 255, 255]
                vertex_colors.append(color)

            # Store UVs (only first occurrence per vertex per layer)
            for layer_idx, uv_layer in enumerate(uv_layers):
                u, v = uv_layer.uv[loop_index].vector.to_tuple(7)
                uv_layers_data[layer_idx][vert_index] = (u, 1 - v)

        idx.append(triangle_indices)

    if calc_tangents:
        mesh.calc_tangents()
        for loop_tri in triangles:
            loop_indices = loop_tri.loops
            for loop_index in loop_indices:
                loop = mesh.loops[loop_index]
                vert_index = loop.vertex_index
                tangents[vert_index]= (*loop.tangent.to_tuple(7), loop.bitangent_sign)
        mesh.free_tangents()

    # Store vertex attributes
    for vertex in mesh.vertices:
        vertices.append(vertex.co.to_tuple(7))
        # vertex_normals.append(vertex.normal.to_tuple())
        if color_layer_type == "POINT":
            color = _get_color_at(_color_buf, vertex.index) if _color_buf else [255, 255, 255, 255]
            vertex_colors.append(color)

        # Ensure UV layers align with vertex indices
        # for layer_idx in range(num_uvs_required):
        #     uv_layers_data[layer_idx].append(vertex_uv_map[layer_idx].get(vertex.index, [0.0, 0.0]))

        if not is_skinned: continue

        _bone_data = []

        for group in vertex.groups:
            vgroup = vertex_groups[group.group]
            bone = get_bone_by_name(vgroup.name, armature)
            if bone is None:
                continue
            _bone_data.append((bone.attr.index, group.weight))

        _bone_data.sort(key=lambda x: x[1], reverse=True)

        _bone_data = _bone_data[:4]

        # Pad with any index and 0.0 if fewer than 4 influences
        # I used the first encountered index for consistency with OpenIV.
        while len(_bone_data) < 4:
            _bone_data.append((fill_index, 0.0))

        _bone_data[0], _bone_data[2] = _bone_data[2], _bone_data[0]

        bone_indices.append([i for i, _ in _bone_data])
        bone_weights.append([w for _, w in _bone_data])

    if not keep_tmp_mesh:
        bpy.ops.object.delete(use_global=False, confirm=False)

    return {
        "Indices": idx,
        "Vertices": vertices,
        "VertxNormals": vertex_normals,
        "BlendWeight": bone_weights,
        "BlendIndices": bone_indices,
        "VertxColors": vertex_colors,
        "Tangents": tangents,
        "UVCoords": uv_layers_data,
    }


def calc_bounds(bound_boxes: list) -> list:
    bounds = []
    max_x_list, max_y_list, max_z_list = [], [], []
    min_x_list, min_y_list, min_z_list = [], [], []

    num_bound_boxes = 0

    for bound_box in bound_boxes:
        center, radius, max_coord, min_coord = calc_bound_sphere_spec(bound_box)
        bounds.append([*center, radius])

        max_x_list.append(max_coord[0])
        max_y_list.append(max_coord[1])
        max_z_list.append(max_coord[2])

        min_x_list.append(min_coord[0])
        min_y_list.append(min_coord[1])
        min_z_list.append(min_coord[2])
        num_bound_boxes += 1

    # Now calculate enclosing bounding sphere, if bound_boxes(meshes) are more than 1
    if num_bound_boxes < 2:
        return bounds

    max_x = max(max_x_list)
    max_y = max(max_y_list)
    max_z = max(max_z_list)

    min_x = min(min_x_list)
    min_y = min(min_y_list)
    min_z = min(min_z_list)

    center, radius = calc_center_n_radius(max_x, max_y, max_z, min_x, min_y, min_z)
    bounds = [[*center, radius]] + bounds

    return bounds


def calc_bound_sphere_spec(
    bound_box,
) -> tuple[tuple[float, float, float], float, tuple[float, float, float], tuple[float, float, float]]:
    max_x = max(bound_box[0][0], bound_box[4][0])
    max_y = max(bound_box[0][1], bound_box[3][1])
    max_z = max(bound_box[0][2], bound_box[1][2])

    min_x = min(bound_box[0][0], bound_box[4][0])
    min_y = min(bound_box[0][1], bound_box[3][1])
    min_z = min(bound_box[0][2], bound_box[1][2])

    center, radius = calc_center_n_radius(max_x, max_y, max_z, min_x, min_y, min_z)

    return center, radius, (max_x, max_y, max_z), (min_x, min_y, min_z)


def calc_center_n_radius(max_x, max_y, max_z, min_x, min_y, min_z) -> tuple[tuple[float, float, float], float]:
    center = ((max_x + min_x) / 2, (max_y + min_y) / 2, (max_z + min_z) / 2)

    radius = (Vector((max_x, max_y, max_z)) - Vector(center)).length

    return center, radius


def iter_sel_objs(sel_objs) -> tuple[int, bool]:
    def _iter(objs: list, _num_meshes = 0, _is_skinned = False):
        for obj in objs:
            if obj.type == "MESH":
                _num_meshes += 1
                _is_skinned |= len(obj.vertex_groups) > 1
            else:
                _num_meshes, __is_skinned = _iter(obj.children, _num_meshes, _is_skinned)
                _is_skinned |= __is_skinned

        return _num_meshes, _is_skinned
    return _iter(sel_objs)



class EXPORT_OT_IV_Mesh(Operator, ExportHelper):
    bl_idname = "iv.export_meshs"
    bl_label = "Export .mesh(IV)"

    filepath: StringProperty()
    filename_ext = ".mesh"

    filter_glob: StringProperty(default="*.mesh", options={"HIDDEN"})
    calc_tangents: BoolProperty(name="Calculate Tangents")

    keep_tmp_mesh: BoolProperty(name="Keep temporary mesh copy")

    def execute(self, context):
        sel_objs = context.selected_objects
        active_obj = sel_objs[0]
        num_meshes, is_skinned = iter_sel_objs(sel_objs)

        bpy.ops.object.select_all(action='DESELECT')
        context.window_manager.progress_begin(0, num_meshes)

        data = {"Version": "11 13", "Skinned": is_skinned, "Geometries": []}

        objs = []
        for obj in sel_objs:
            if obj.type == "MESH":
                if obj.hide_get():
                    self.report({"WARNING"}, f"{obj.name} is hidden in active view_layer {context.view_layer.name}, and hence not selectable, skipping export")
                    continue
                objs.append(obj)
            elif obj.type in ("ARMATURE", "EMPTY"):
                for child in obj.children_recursive:
                    if child.type == "MESH":
                        if child.hide_get():
                            self.report({"WARNING"},
                                        f"{child.name} is hidden in active view_layer {context.view_layer.name}, and hence not selectable, skipping export")
                            continue
                        objs.append(child)

        bounds = []
        for obj in objs:
            bounds.append(obj.bound_box)
            export(self, data, obj, calc_tangents=self.calc_tangents, keep_tmp_mesh=self.keep_tmp_mesh)
            context.window_manager.progress_update(1)

        if not is_skinned:
            data["Bounds"] = calc_bounds(bounds)

        with open(self.filepath, "w") as writer:
            writer.write(gta_iv_dict_to_mesh(data))

        active_obj.select_set(True)
        for obj in sel_objs:
            obj.select_set(True)
        context.window_manager.progress_end()
        return {"FINISHED"}

    def invoke(self, context, event):
        sel_objs = context.selected_objects
        
        has_mesh = any(
            obj.type == "MESH" or any(child.type == "MESH" for child in obj.children_recursive) for obj in sel_objs
        )

        if not has_mesh:
            self.report({"ERROR"}, "No selectable mesh found in selection")
            return {"CANCELLED"}

        filepath: str | None = None
        obj = sel_objs[0]
        if obj.parent is not None and "filepath" in obj.parent:
            filepath = obj.parent.get("filepath")
        elif "filepath" in obj:
            filepath = obj.get("filepath")

        if filepath is not None:
            if filepath.endswith(self.filename_ext):
                self.filepath = filepath
            else:
                parent_folder = dirname(filepath)
                dir_name = basename(filepath).rsplit(".", maxsplit=1)[0]
                self.filepath = join_path(parent_folder, dir_name, dir_name) + self.filename_ext

        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.prop(self, "calc_tangents")
        panel_header, panel_body = box.panel(self.bl_idname + "_unknown_flags", default_closed=True)
        panel_header.label(text="Advanced")
        if panel_body is not None:
            grid_flow = panel_body.grid_flow(row_major=True, columns=2, align=True)
            grid_flow.prop(self, "keep_tmp_mesh")

def register():
    try_unregister_class(EXPORT_OT_IV_Mesh)
    bpy.utils.register_class(EXPORT_OT_IV_Mesh)


def unregister():
    bpy.utils.unregister_class(EXPORT_OT_IV_Mesh)


