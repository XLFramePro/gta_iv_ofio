"""
GTA IV openFormats .obn (phBound) importer for Blender 4.x

Format:
  Version 32 11
  phBound { Type BoundComposite  Children N { phBound 0 { Type BoundBVH
    VertexScale sx sy sz  VertexOffset ox oy oz
    Polygons N { Polygon i { Material M  Vertices i0 i1 i2 [i3] } ... }
    Vertices N { ix iy iz  ... }
    Materials N { type flags flags2  ... }
  } ... }  ChildTransforms N { Matrix i { row0 row1 row2 translation } ... } }

Vertices: world_pos = int_value * VertexScale + VertexOffset
"""

from pathlib import Path
from time import time

import bpy
import orjson
from bpy.props import StringProperty, CollectionProperty, BoolProperty
from bpy.types import Operator, PropertyGroup
from bpy_extras.io_utils import ImportHelper
from mathutils import Matrix, Vector

from ..blender_utils import create_empty_obj, try_unregister_class

# ─── GTA IV collision material names ─────────────────────────────────────────
_MATERIAL_NAMES = {
    0: "DEFAULT", 3: "TARMAC", 12: "PAVEMENT", 14: "STONE_STEPS",
    20: "SAND_DEEP", 21: "DIRT", 22: "TARMAC_DIRT", 23: "METAL_FLOOR",
    27: "METAL_SOLID", 30: "GLASS", 31: "WOOD_SOLID", 32: "CONCRETE",
    33: "RUBBER", 34: "CARPET", 54: "GRASS", 59: "WATER",
    63: "LEAVES", 64: "GRAVEL", 70: "STEEL_RAILING", 71: "TARP",
    84: "TARMAC_PAINTED", 89: "SOIL",
}

def _mat_name(mat_id):
    return _MATERIAL_NAMES.get(mat_id, f"COL_MAT_{mat_id}")

# ─── Parser ───────────────────────────────────────────────────────────────────

def _parse_obn_text(text):
    """Parse .obn text into nested Python structure."""
    lines = text.splitlines()
    pos = [0]

    def peek():
        while pos[0] < len(lines):
            line = lines[pos[0]].strip()
            if line and not line.startswith("//"):
                return line
            pos[0] += 1
        return None

    def consume():
        line = peek()
        pos[0] += 1
        return line

    def parse_block():
        """Parse { ... } block into list of (key, value) pairs."""
        entries = []
        consume()  # opening {
        while True:
            line = peek()
            if line is None or line == "}":
                consume()
                break
            tokens = line.split()
            keyword = tokens[0]
            consume()
            next_line = peek()
            if next_line == "{":
                sub = parse_block()
                entries.append((keyword, {"__tokens__": tokens[1:], "__children__": sub}))
            else:
                entries.append((keyword, tokens[1:]))
        return entries

    def parse_root():
        version_line = consume()  # "Version 32 11"
        consume()                  # "phBound"
        return {"Version": version_line, "phBound": parse_block()}

    return parse_root()


def _as_dict(entries):
    """Convert [(key, val), ...] to {key: val} (last-write-wins)."""
    return {k: v for k, v in entries}


# ─── Vertex block reading ─────────────────────────────────────────────────────
# Inside parse_block, a line "27391 -23201 -3629" becomes entry:
#   keyword="27391", value=["-23201", "-3629"]
# We reconstruct the full token list from that.

def _read_vertex_block(entries, vs, vo):
    """Decode compressed integer vertices → list of (x,y,z) world-space tuples."""
    verts = []
    for key, val in entries:
        if key == "Vertices" and isinstance(val, dict):
            for kw, rest in val["__children__"]:
                # kw = first int token as string, rest = remaining tokens
                try:
                    tokens = [kw] + rest
                    if len(tokens) >= 3:
                        ix, iy, iz = int(tokens[0]), int(tokens[1]), int(tokens[2])
                        verts.append((
                            ix * vs[0] + vo[0],
                            iy * vs[1] + vo[1],
                            iz * vs[2] + vo[2],
                        ))
                except (ValueError, TypeError):
                    continue
            break
    return verts


# ─── Material helper ──────────────────────────────────────────────────────────

def _get_col_mat(mat_id, mat_cache):
    if mat_id in mat_cache:
        return mat_cache[mat_id]
    name = f"COL_{_mat_name(mat_id)}"
    mat = bpy.data.materials.get(name)
    if mat is None:
        import hashlib
        mat = bpy.data.materials.new(name=name)
        mat.use_nodes = True
        h = hashlib.md5(name.encode()).digest()
        bsdf = mat.node_tree.nodes.get("Principled BSDF")
        if bsdf:
            bsdf.inputs["Base Color"].default_value = (h[0]/255, h[1]/255, h[2]/255, 1.0)
            bsdf.inputs["Alpha"].default_value = 0.5
        if hasattr(mat, "surface_render_method"):
            mat.surface_render_method = "BLENDED"
        elif hasattr(mat, "blend_method"):
            mat.blend_method = "BLEND"
    mat_cache[mat_id] = mat
    return mat


# ─── BoundBVH → Blender mesh ─────────────────────────────────────────────────

def _build_mesh(child_entries, name, collection, mat_cache):
    """
    Build and link a Blender mesh object from BoundBVH entries.
    The mesh is placed at world origin; vertices carry their world coordinates.
    Returns the object or None.
    """
    d = _as_dict(child_entries)

    # Vertex scale / offset
    try:
        vs = [float(x) for x in d.get("VertexScale", ["1", "1", "1"])]
        vo = [float(x) for x in d.get("VertexOffset", ["0", "0", "0"])]
    except (ValueError, TypeError):
        vs, vo = [1.0, 1.0, 1.0], [0.0, 0.0, 0.0]

    verts_3d = _read_vertex_block(child_entries, vs, vo)
    if not verts_3d:
        return None

    n_verts = len(verts_3d)

    # Build face list from Polygons block
    faces = []
    face_mat_ids = []

    poly_entry = d.get("Polygons")
    if isinstance(poly_entry, dict):
        for pk, pv in poly_entry["__children__"]:
            if pk != "Polygon":
                continue
            pd = _as_dict(pv["__children__"])
            try:
                mat_id = int(pd.get("Material", ["0"])[0])
            except (ValueError, TypeError, IndexError):
                mat_id = 0

            vt = pd.get("Vertices", [])
            try:
                vi = [int(t) for t in vt]
            except (ValueError, TypeError):
                continue

            # GTA IV: 4 indices where 4th == 0 → triangle (except when vertex 0 is valid)
            if len(vi) >= 4 and vi[3] == 0 and vi[0] != 0:
                face = (vi[0], vi[1], vi[2])
            elif len(vi) >= 4:
                face = tuple(vi[:4])
            elif len(vi) == 3:
                face = tuple(vi)
            else:
                continue

            # Clamp to valid range
            face = tuple(min(max(i, 0), n_verts - 1) for i in face)
            faces.append(face)
            face_mat_ids.append(mat_id)

    # Create mesh data
    mesh = bpy.data.meshes.new(name)
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)

    mesh.from_pydata(verts_3d, [], faces)
    mesh.validate(verbose=False)
    mesh.update()

    # Assign collision materials
    slot_map = {}
    for fid, mat_id in enumerate(face_mat_ids):
        if fid >= len(mesh.polygons):
            break
        if mat_id not in slot_map:
            mat = _get_col_mat(mat_id, mat_cache)
            obj.data.materials.append(mat)
            slot_map[mat_id] = len(obj.data.materials) - 1
        mesh.polygons[fid].material_index = slot_map[mat_id]

    obj["col_type"] = "BoundBVH"
    return obj


# ─── Main import function ─────────────────────────────────────────────────────

def import_obn(self, filepath: Path) -> int:
    text = filepath.read_text(encoding="utf-8", errors="replace")
    parsed = _parse_obn_text(text)

    root_entries = parsed.get("phBound", [])
    root_dict = _as_dict(root_entries)

    filename = filepath.name
    collection = bpy.context.collection

    # Root empty
    root_empty = create_empty_obj(filename)
    root_empty["filepath"] = str(filepath)
    root_empty.empty_display_size = 1.0

    children_entry = root_dict.get("Children")
    if not isinstance(children_entry, dict):
        return 0

    child_list = children_entry.get("__children__", [])
    mat_cache = {}
    child_idx = 0
    count = 0

    for key, val in child_list:
        if key != "phBound":
            continue

        child_entries = val.get("__children__", [])
        child_dict = _as_dict(child_entries)
        type_tokens = child_dict.get("Type", ["Unknown"])
        bound_type = " ".join(type_tokens) if isinstance(type_tokens, list) else str(type_tokens)

        child_name = f"{filename}.{child_idx}"

        if bound_type in ("BoundBVH", "BoundGeometry"):
            obj = _build_mesh(child_entries, child_name, collection, mat_cache)
            if obj is not None:
                # Parent with matrix_parent_inverse so world position is preserved
                obj.parent = root_empty
                obj.matrix_parent_inverse = root_empty.matrix_world.inverted()
                count += 1
        else:
            # Unsupported child type → empty placeholder
            empty = create_empty_obj(child_name)
            empty["col_type"] = bound_type
            empty.parent = root_empty
            empty.matrix_parent_inverse = root_empty.matrix_world.inverted()

        child_idx += 1

    return count


# ─── Operator ─────────────────────────────────────────────────────────────────

class ImportGTAIVOBN(Operator, ImportHelper):
    """Imports a GTA IV openFormats .obn collision (phBound) file"""

    bl_idname = "gta4_ofio.import_bound"
    bl_label = "Import .obn (IV)"

    filename_ext = ".obn"
    filter_glob: StringProperty(default="*.obn", options={"HIDDEN"})
    files: CollectionProperty(type=PropertyGroup)

    def execute(self, context):
        folder = Path(self.filepath).parent
        count = 0
        time_start = time()
        for selection in self.files:
            fp = folder / selection.name
            try:
                count += import_obn(self, fp)
            except Exception as e:
                import traceback
                self.report({"ERROR"}, f"{selection.name}: {e}\n{traceback.format_exc()}")
        time_spent = time() - time_start
        self.report({"INFO"}, f"Imported {count} collision mesh(es) in {time_spent:.4f}sec")
        return {"FINISHED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


def register():
    try_unregister_class(ImportGTAIVOBN)
    bpy.utils.register_class(ImportGTAIVOBN)


def unregister():
    bpy.utils.unregister_class(ImportGTAIVOBN)
