"""
Max Payne 3 binary .wbn importer for Blender 4.x  (RSC05 / RAGE phBound)

RSC05 layout (zlib-compressed from offset 12, version=40):
  Only system segment used (no gfx segment for collision data).
  Virtual pointer: 0x50xxxxxx → decompressed[ptr & 0x00FFFFFF]

Collision hierarchy:
  phBoundComposite  (type=0x0A, at decompressed[0])
    [0x08]  bound_type   (u8)
    [0x20]  AABB_min     (f32×3)
    [0x30]  AABB_max     (f32×3)
    [0x70]  children_ptr → array of N phBound ptrs
    [0x74]  local_matrices_ptr
    [0x88]  child_count  (u16)

  phBoundBVH  (type=0x08, each child)
    phBound base:
      [0x08]  bound_type (u8)
      [0x20]  AABB_min   (f32×3)
      [0x30]  AABB_max   (f32×3)
    phBoundGeometry:
      [0x7C]  polygons_ptr → polygon array
      [0x78]  hi16 = poly_count
      [0xA0]  vertices_ptr → vertex array
      [0xA4]  poly_count  (u32)
      [0xA8]  vert_count  (u32)

  Vertex format: 3 × u16  (stride=6)
    Dequantize: pos = AABB_min + (u16 / 65535.0) × (AABB_max - AABB_min)

  Polygon format: f32 + 6 × u16  (stride=16)
    [0..3]  face_area_or_normal_val  (f32, ignored)
    [4..5]  vertex_index_0  (u16)
    [6..7]  vertex_index_1  (u16)
    [8..9]  vertex_index_2  (u16)
    [10..11] material_index (u16)
    [12..13] adj_edge_0     (u16, ignored)
    [14..15] adj_edge_1     (u16, ignored)
"""

from pathlib import Path
import struct
import zlib
from time import time

import bpy
from bpy.props import StringProperty, CollectionProperty
from bpy.types import Operator, PropertyGroup
from bpy_extras.io_utils import ImportHelper

from ..blender_utils import create_empty_obj, try_unregister_class


# ─── Constants ────────────────────────────────────────────────────────────────

RSC05_MAGIC         = b"RSC\x05"
WBN_VERSION         = 40
BOUND_COMPOSITE     = 0x0A
BOUND_BVH           = 0x08
BOUND_GEOMETRY      = 0x04


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _rp(d, o):   return struct.unpack_from("<I", d, o)[0] if o + 4 <= len(d) else 0
def _ru16(d, o): return struct.unpack_from("<H", d, o)[0] if o + 2 <= len(d) else 0
def _rf32(d, o): return struct.unpack_from("<f", d, o)[0] if o + 4 <= len(d) else 0.0

def _resolve(ptr):
    """Resolve a 0x50xxxxxx virtual pointer to a physical offset."""
    if (ptr >> 24) == 0x50:
        return ptr & 0x00FFFFFF
    return None


# ─── RSC05 container ──────────────────────────────────────────────────────────

def _decompress(raw: bytes) -> bytes:
    if raw[:4] != RSC05_MAGIC:
        raise ValueError(f"Not an RSC05 file (magic={raw[:4]})")
    version = struct.unpack_from("<I", raw, 4)[0]
    if version != WBN_VERSION:
        raise ValueError(
            f"Unsupported WBN version {version} (expected {WBN_VERSION}).\n"
            f"If this is a GTA IV .wbn, use Import .obn (IV Collision)."
        )
    return zlib.decompress(raw[12:])


# ─── Geometry readers ─────────────────────────────────────────────────────────

def _read_aabb(dec, off):
    """Read AABB min/max from phBound base."""
    mn = (
        _rf32(dec, off + 0x20),
        _rf32(dec, off + 0x24),
        _rf32(dec, off + 0x28),
    )
    mx = (
        _rf32(dec, off + 0x30),
        _rf32(dec, off + 0x34),
        _rf32(dec, off + 0x38),
    )
    return mn, mx


def _dequant(raw_x, raw_y, raw_z, mn, mx):
    """Dequantize u16 vertex to float using AABB."""
    x = mn[0] + (raw_x / 65535.0) * (mx[0] - mn[0])
    y = mn[1] + (raw_y / 65535.0) * (mx[1] - mn[1])
    z = mn[2] + (raw_z / 65535.0) * (mx[2] - mn[2])
    return x, y, z


def _scan_true_vert_count(dec, verts_off, reported_vc, mn, mx, max_scan=8):
    """
    The vertex buffer contains MORE entries than reported in +0xA8.
    Scan forward until a vertex falls outside the AABB (+ margin).
    Returns the actual number of valid vertices in the buffer.
    """
    margin = 2.0
    true_vc = 0
    for i in range(reported_vc * max_scan):
        v_off = verts_off + i * 6
        if v_off + 6 > len(dec):
            break
        vx, vy, vz = struct.unpack_from("<HHH", dec, v_off)
        x = mn[0] + (vx / 65535.0) * (mx[0] - mn[0])
        y = mn[1] + (vy / 65535.0) * (mx[1] - mn[1])
        z = mn[2] + (vz / 65535.0) * (mx[2] - mn[2])
        if (mn[0] - margin <= x <= mx[0] + margin and
                mn[1] - margin <= y <= mx[1] + margin and
                mn[2] - margin <= z <= mx[2] + margin):
            true_vc = i + 1
        else:
            break
    return true_vc


def _read_bound_bvh(dec, off):
    """
    Parse a phBoundBVH/phBoundGeometry and return (verts, faces) or None.
    """
    bound_type = dec[off + 0x08] if off + 0x08 < len(dec) else 0
    if bound_type not in (BOUND_BVH, BOUND_GEOMETRY):
        return None

    mn, mx = _read_aabb(dec, off)

    # Polygon array: ptr at +0x7C, count in hi16 of +0x78
    polys_ptr   = _rp(dec, off + 0x7C)
    poly_count  = (_rp(dec, off + 0x78) >> 16) & 0xFFFF
    polys_off   = _resolve(polys_ptr)

    # Vertex array: ptr at +0xA0, reported count at +0xA8
    # NOTE: +0xA8 under-reports the true vertex count.
    # The actual buffer contains more vertices (typically N× reported)
    # used for hard-edge duplication. We scan to find the true count.
    verts_ptr    = _rp(dec, off + 0xA0)
    reported_vc  = _rp(dec, off + 0xA8)
    verts_off    = _resolve(verts_ptr)

    # Fallback: poly_count may also be at +0xA4
    if poly_count == 0:
        poly_count = _rp(dec, off + 0xA4)

    if not (polys_off and verts_off and poly_count > 0 and reported_vc > 0):
        return None

    # Find the real vertex count by scanning the buffer
    true_vc = _scan_true_vert_count(dec, verts_off, reported_vc, mn, mx)
    if true_vc == 0:
        true_vc = reported_vc  # fallback

    # ── Vertices (u16×3, stride=6) ──
    verts = []
    for i in range(true_vc):
        v_off = verts_off + i * 6
        if v_off + 6 > len(dec):
            break
        vx, vy, vz = struct.unpack_from("<HHH", dec, v_off)
        verts.append(_dequant(vx, vy, vz, mn, mx))

    if not verts:
        return None

    # ── Polygons (f32 + u16×6, stride=16) ──
    # Some children have garbage data after valid polygons;
    # skip any triangle whose indices fall outside the vertex buffer.
    n = len(verts)
    faces = []
    for i in range(poly_count):
        p_off = polys_off + i * 16
        if p_off + 10 > len(dec):
            break
        v0 = _ru16(dec, p_off + 4)
        v1 = _ru16(dec, p_off + 6)
        v2 = _ru16(dec, p_off + 8)
        if v0 < n and v1 < n and v2 < n:
            faces.append((v0, v1, v2))

    if not faces:
        return None

    return verts, faces


# ─── Blender mesh builder ─────────────────────────────────────────────────────

def _build_mesh(name: str, verts, faces, collection,
                mat: bpy.types.Material | None = None) -> bpy.types.Object | None:
    mesh = bpy.data.meshes.new(name)
    obj  = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)

    mesh.from_pydata(verts, [], faces)

    if mat:
        obj.data.materials.append(mat)

    mesh.validate(verbose=False)
    mesh.update()
    return obj


def _get_collision_material() -> bpy.types.Material:
    """Return (or create) a shared semi-transparent green collision material."""
    mat_name = "MP3_Collision"
    if mat_name in bpy.data.materials:
        return bpy.data.materials[mat_name]
    mat = bpy.data.materials.new(name=mat_name)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    output.location = (300, 0)
    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (0, 0)
    bsdf.inputs["Base Color"].default_value = (0.0, 1.0, 0.2, 1.0)  # green
    bsdf.inputs["Alpha"].default_value = 0.35
    if hasattr(mat, "surface_render_method"):
        mat.surface_render_method = "BLENDED"
    elif hasattr(mat, "blend_method"):
        mat.blend_method = "BLEND"
    mat.node_tree.links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])
    return mat


# ─── Main importer ────────────────────────────────────────────────────────────

def import_mp3_wbn(self, filepath: Path) -> int:
    raw  = filepath.read_bytes()
    dec  = _decompress(raw)
    coll = bpy.context.collection

    bound_type = dec[0x08] if len(dec) > 0x08 else 0

    root_empty = create_empty_obj(filepath.name)
    root_empty["filepath"] = str(filepath)

    mat = _get_collision_material()
    count = 0

    if bound_type == BOUND_COMPOSITE:
        # ── phBoundComposite: iterate children ──
        children_ptr = _rp(dec, 0x70)
        children_off = _resolve(children_ptr)
        child_count  = _ru16(dec, 0x88)

        if not children_off or child_count == 0:
            raise ValueError("phBoundComposite: no children found")

        for ci in range(child_count):
            child_ptr = _rp(dec, children_off + ci * 4)
            child_off = _resolve(child_ptr)
            if child_off is None:
                continue

            result = _read_bound_bvh(dec, child_off)
            if result is None:
                continue

            verts, faces = result
            name = f"{filepath.stem}_col{ci}"
            obj  = _build_mesh(name, verts, faces, coll, mat)
            if obj:
                obj.parent = root_empty
                obj.matrix_parent_inverse = root_empty.matrix_world.inverted()
                obj.display_type = "WIRE"
                count += 1

    elif bound_type in (BOUND_BVH, BOUND_GEOMETRY):
        # ── Single phBoundBVH ──
        result = _read_bound_bvh(dec, 0)
        if result:
            verts, faces = result
            obj = _build_mesh(f"{filepath.stem}_col", verts, faces, coll, mat)
            if obj:
                obj.parent = root_empty
                obj.matrix_parent_inverse = root_empty.matrix_world.inverted()
                obj.display_type = "WIRE"
                count += 1
    else:
        raise ValueError(f"Unsupported phBound type 0x{bound_type:02X}")

    return count


# ─── Operator ─────────────────────────────────────────────────────────────────

class ImportMP3WBN(Operator, ImportHelper):
    """Imports Binary Collision Resource"""

    bl_idname    = "mp3_ofio.import_wbn"
    bl_label     = "Import .wbn"
    filename_ext = ".wbn"
    filter_glob: StringProperty(default="*.wbn", options={"HIDDEN"})
    files: CollectionProperty(type=PropertyGroup)

    def execute(self, context):
        folder = Path(self.filepath).parent
        count  = 0
        t      = time()
        for sel in self.files:
            fp = folder / sel.name
            try:
                count += import_mp3_wbn(self, fp)
            except Exception as e:
                import traceback
                self.report({"ERROR"}, f"{sel.name}: {e}\n{traceback.format_exc()}")
        self.report({"INFO"}, f"Imported {count} collision mesh(es) in {time()-t:.4f}sec")
        return {"FINISHED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


def register():
    try_unregister_class(ImportMP3WBN)
    bpy.utils.register_class(ImportMP3WBN)


def unregister():
    bpy.utils.unregister_class(ImportMP3WBN)
