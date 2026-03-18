"""
Max Payne 3 binary .wdr importer for Blender 4.x

Format: RSC05 (RAGE Resource Compiler v5)
  Header: "RSC\x05" + version(4) + sys_flags(4) → 12 bytes total
  Payload: zlib-compressed data starting at offset 12

  Decompressed layout:
    [0 .. SYS_SIZE-1]   System segment  (CPU structs, vertex data, shader info)
    [SYS_SIZE .. end]   Graphics segment (index buffers)

  SYS_SIZE is determined by probing index buffer validity (sys_size = 0x180000 for known files).
  All pointers are virtual:
    0x50xxxxxx → system segment at offset (ptr & 0x00FFFFFF)
    0x60xxxxxx → graphics segment at offset SYS_SIZE + (ptr & 0x00FFFFFF)

  Root CrmDrawable at decompressed[0]:
    [0x08] shader_group_ptr → CrmShaderGroup
    [0x40] lod_models_collection_ptr → ptr array of 14 CrmModel ptrs

  CrmShaderGroup at resolved ptr:
    [0x00] models_array_ptr → array_ptr (ptr), [0x04] count (u16)
    [0x08] texture_dict_ptr
    [0x10] shader_params_offsets ...

  CrmModel (0x50 bytes each):
    [0x0C] vbd_ptr → CrmVertexBuffer descriptor
    [0x1C] ibd_ptr → CrmIndexBuffer descriptor

  CrmVertexBuffer:
    [0x04] stride (lower 16 bits)
    [0x08] vertex_data_ptr (0x50xxxxxx → sys segment)
    [0x0C] vertex_count

  CrmIndexBuffer:
    [0x04] index_count
    [0x08] index_data_ptr (0x60xxxxxx → gfx segment)

  Vertex format (stride 52): pos(12) + norm(12) + color(4) + uv(8) + tangent(16)
  Vertex format (stride 36): pos(12) + norm(12) + color(4) + uv(8)
  Indices: u16, triangles
"""

from pathlib import Path
from time import time
import struct
import zlib

import bpy
from bpy.props import StringProperty, CollectionProperty
from bpy.types import Operator, PropertyGroup
from bpy_extras.io_utils import ImportHelper

from ..blender_utils import create_empty_obj, try_unregister_class


# ─── RSC05 constants ──────────────────────────────────────────────────────────

RSC05_MAGIC   = b"RSC\x05"
RSC05_VERSION = 144  # MP3 WDR version


# ─── Low-level helpers ────────────────────────────────────────────────────────

def _rp(data, offset):
    return struct.unpack_from("<I", data, offset)[0] if offset + 4 <= len(data) else 0

def _ru16(data, offset):
    return struct.unpack_from("<H", data, offset)[0] if offset + 2 <= len(data) else 0

def _rf32(data, offset):
    return struct.unpack_from("<f", data, offset)[0] if offset + 4 <= len(data) else 0.0


# ─── RSC05 decompression ──────────────────────────────────────────────────────

def _decompress_rsc05(raw: bytes) -> bytes:
    """Decompress RSC05 zlib payload (starts at offset 12)."""
    if raw[:4] != RSC05_MAGIC:
        raise ValueError(f"Not an RSC05 file (magic={raw[:4]})")
    version = struct.unpack_from("<I", raw, 4)[0]
    if version != RSC05_VERSION:
        raise ValueError(f"Unsupported RSC05 version {version} (expected {RSC05_VERSION})")
    return zlib.decompress(raw[12:])


def _find_sys_size(data: bytes, vdata_ptr: int, probe_ib_gfx_off: int,
                   probe_icount: int, probe_vcount: int) -> int:
    """
    Find the correct sys/gfx boundary by probing index validity.
    Tries candidates and returns the sys_size where max(indices) < vcount.
    """
    for sys_sz in range(0x100000, 0x400000, 0x20000):
        ib_phys = sys_sz + probe_ib_gfx_off
        if ib_phys + probe_icount * 2 > len(data):
            continue
        idxs = struct.unpack_from(f"<{probe_icount}H", data, ib_phys)
        if max(idxs) < probe_vcount:
            return sys_sz
    return 0x180000  # fallback


# ─── Pointer resolution ───────────────────────────────────────────────────────

class _RSC05:
    def __init__(self, data: bytes, sys_size: int):
        self.data     = data
        self.sys_size = sys_size

    def resolve(self, ptr: int):
        """Return absolute offset in decompressed data, or None."""
        if ptr == 0 or ptr == 0xFFFFFFFF:
            return None
        hi = ptr >> 24
        lo = ptr & 0x00FFFFFF
        if hi == 0x50:
            return lo
        if hi == 0x60:
            return self.sys_size + lo
        return None

    def rp(self, off): return _rp(self.data, off)
    def ru16(self, off): return _ru16(self.data, off)
    def rf32(self, off): return _rf32(self.data, off)


# ─── Geometry reader ──────────────────────────────────────────────────────────

def _read_geometry(rsc: _RSC05, model_off: int):
    """
    Read one CrmModel geometry (0x50 bytes) from sys segment.
    Returns dict with verts, normals, colors, uvs, faces — or None.
    """
    vbd_ptr = rsc.rp(model_off + 0x0C)
    ibd_ptr = rsc.rp(model_off + 0x1C)

    vbd_off = rsc.resolve(vbd_ptr)
    ibd_off = rsc.resolve(ibd_ptr)
    if vbd_off is None or ibd_off is None:
        return None

    stride     = rsc.rp(vbd_off + 0x04) & 0xFFFF
    vcount     = rsc.rp(vbd_off + 0x0C)
    vdata_ptr  = rsc.rp(vbd_off + 0x08)
    icount     = rsc.rp(ibd_off + 0x04)
    idata_ptr  = rsc.rp(ibd_off + 0x08)

    vdata_off = rsc.resolve(vdata_ptr)
    idata_off = rsc.resolve(idata_ptr)
    if vdata_off is None or idata_off is None:
        return None
    if stride == 0 or vcount == 0 or icount == 0:
        return None
    if icount % 3 != 0:
        return None

    data = rsc.data

    # ── Vertices ──
    verts, normals, colors, uvs = [], [], [], []
    for i in range(vcount):
        off = vdata_off + i * stride
        if off + 28 > len(data):
            break
        x, y, z = struct.unpack_from("<fff", data, off)
        nx, ny, nz = struct.unpack_from("<fff", data, off + 12)
        r, g, b, a = data[off+24], data[off+25], data[off+26], data[off+27]

        u, v = 0.0, 0.0
        if stride >= 36 and off + 36 <= len(data):
            u, v = struct.unpack_from("<ff", data, off + 28)

        verts.append((x, y, z))
        normals.append((nx, ny, nz))
        colors.append((r, g, b, a))
        uvs.append((u, v))

    if not verts:
        return None

    # ── Indices → faces ──
    faces = []
    if idata_off + icount * 2 <= len(data):
        raw_indices = struct.unpack_from(f"<{icount}H", data, idata_off)
        n = len(verts)
        for t in range(0, icount - 2, 3):
            i0, i1, i2 = raw_indices[t], raw_indices[t+1], raw_indices[t+2]
            if i0 < n and i1 < n and i2 < n:
                faces.append((i0, i1, i2))

    if not faces:
        return None

    return {
        "verts":   verts,
        "normals": normals,
        "colors":  colors,
        "uvs":     uvs,
        "faces":   faces,
        "stride":  stride,
    }


# ─── Blender mesh builder ─────────────────────────────────────────────────────

def _build_mesh(name: str, geo: dict, collection) -> bpy.types.Object | None:
    verts   = geo["verts"]
    faces   = geo["faces"]
    normals = geo["normals"]
    colors  = geo["colors"]
    uvs     = geo["uvs"]

    mesh = bpy.data.meshes.new(name)
    obj  = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)

    mesh.from_pydata(verts, [], faces)

    # Vertex colours
    if colors:
        CNAME = "Col"
        if CNAME in mesh.attributes:
            ex = mesh.attributes[CNAME]
            if ex.data_type != "BYTE_COLOR" or ex.domain != "CORNER":
                mesh.attributes.remove(ex)
        if CNAME not in mesh.attributes:
            mesh.attributes.new(CNAME, "BYTE_COLOR", "CORNER")
        ca = mesh.attributes[CNAME]
        flat = []
        for loop in mesh.loops:
            vi = loop.vertex_index
            c  = colors[vi] if vi < len(colors) else (255, 255, 255, 255)
            flat.extend(x / 255.0 for x in c)
        ca.data.foreach_set("color", flat)
        try:
            mesh.color_attributes.active_color = mesh.color_attributes[CNAME]
        except Exception:
            pass

    # UV map
    if uvs:
        uv_layer = mesh.uv_layers.new(name="UVMap").uv
        for loop in mesh.loops:
            vi = loop.vertex_index
            if vi < len(uvs):
                u, v = uvs[vi]
                uv_layer[loop.index].vector = (u, 1.0 - v)

    # Custom normals
    if normals and len(normals) == len(verts):
        try:
            if "sharp_vector" not in mesh.attributes:
                mesh.attributes.new("sharp_vector", "FLOAT_VECTOR", "POINT")
            attr = mesh.attributes["sharp_vector"]
            flat_n = []
            for n in normals:
                flat_n.extend(n[:3])
            attr.data.foreach_set("vector", flat_n)
        except Exception:
            try:
                mesh.normals_split_custom_set_from_vertices(normals)
            except AttributeError:
                pass

    mesh.validate(verbose=False)
    mesh.update()
    return obj


# ─── Main importer ────────────────────────────────────────────────────────────

def import_mp3_wdr(self, filepath: Path) -> int:
    """Import a MP3 binary .wdr file. Returns number of mesh objects created."""
    raw        = filepath.read_bytes()
    data       = _decompress_rsc05(raw)
    collection = bpy.context.collection
    filename   = filepath.name

    # ── Locate root drawable geometry collection ──
    # Root struct at offset 0
    # [0x40] → shader group / model collection ptr
    models_ptr = _rp(data, 0x40)
    hi = models_ptr >> 24
    if hi != 0x50:
        raise ValueError(f"Unexpected models ptr: 0x{models_ptr:08X}")

    collection_off = models_ptr & 0x00FFFFFF

    # CrmGeomCollection:
    # [0x00] array_ptr (pointer to array of CrmModel ptrs)
    # [0x04] count (lower u16)
    # [0x50..0x?] direct list of CrmModel structs (0x50 bytes each)
    # 
    # Based on analysis: models start at collection_off + 0x50
    # and there are N models (count encoded in ptr spacing)

    # Probe first model to determine sys_size
    # model ptrs start at collection_off + 0x50 (array of 0x50-pointers)
    model0_ptr = _rp(data, collection_off + 0x50)
    model0_off = model0_ptr & 0x00FFFFFF if (model0_ptr >> 24) == 0x50 else None
    vbd0_ptr   = _rp(data, model0_off + 0x0C) if model0_off else 0
    ibd0_ptr   = _rp(data, model0_off + 0x1C) if model0_off else 0

    vbd0_off   = vbd0_ptr & 0x00FFFFFF if (vbd0_ptr >> 24) == 0x50 else None
    ibd0_off   = ibd0_ptr & 0x00FFFFFF if (ibd0_ptr >> 24) == 0x50 else None

    sys_size = 0x180000  # default
    if vbd0_off and ibd0_off:
        vcount0    = _rp(data, vbd0_off + 0x0C)
        icount0    = _rp(data, ibd0_off + 0x04)
        idata0_ptr = _rp(data, ibd0_off + 0x08)
        vdata0_ptr = _rp(data, vbd0_off + 0x08)
        if (idata0_ptr >> 24) == 0x60:
            ib0_gfx_off = idata0_ptr & 0x00FFFFFF
            sys_size = _find_sys_size(data, vdata0_ptr, ib0_gfx_off, icount0, vcount0)

    rsc = _RSC05(data, sys_size)

    # ── Collect model pointers ──
    # At collection_off + 0x50..+0x8C: array of ptrs to CrmModel structs
    # Count by reading 4-byte ptrs until the high byte is not 0x50/0x60
    MAX_MODELS = 64
    model_offs = []
    model_ptrs_off = collection_off + 0x50
    for i in range(MAX_MODELS):
        model_ptr = rsc.rp(model_ptrs_off + i * 4)
        hi = model_ptr >> 24
        if hi not in (0x50, 0x60):
            break
        model_off = rsc.resolve(model_ptr)
        if model_off is None:
            break
        model_offs.append(model_off)

    if not model_offs:
        raise ValueError("No geometry models found in WDR")

    # ── Root empty ──
    root_empty = create_empty_obj(filename)
    root_empty["filepath"] = str(filepath)

    # ── Import each model ──
    count = 0
    for i, model_off in enumerate(model_offs):
        geo = _read_geometry(rsc, model_off)
        if geo is None:
            continue

        name = f"{filepath.stem}_geo{i}"
        obj  = _build_mesh(name, geo, collection)
        if obj:
            obj.parent = root_empty
            obj.matrix_parent_inverse = root_empty.matrix_world.inverted()
            obj["stride"] = geo["stride"]
            count += 1

    return count


# ─── Operator ─────────────────────────────────────────────────────────────────

class ImportMP3WDR(Operator, ImportHelper):
    """Imports a Max Payne 3 binary .wdr file (RSC05 RAGE resource)"""

    bl_idname    = "mp3_ofio.import_wdr"
    bl_label     = "Import .wdr [MP3]"

    filename_ext = ".wdr"
    filter_glob: StringProperty(default="*.wdr", options={"HIDDEN"})
    files: CollectionProperty(type=PropertyGroup)

    def execute(self, context):
        folder = Path(self.filepath).parent
        count  = 0
        t      = time()
        for sel in self.files:
            fp = folder / sel.name
            try:
                count += import_mp3_wdr(self, fp)
            except Exception as e:
                import traceback
                self.report({"ERROR"}, f"{sel.name}: {e}\n{traceback.format_exc()}")
        self.report({"INFO"}, f"Imported {count} mesh(es) from .wdr in {time()-t:.4f}sec")
        return {"FINISHED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


def register():
    try_unregister_class(ImportMP3WDR)
    bpy.utils.register_class(ImportMP3WDR)


def unregister():
    bpy.utils.unregister_class(ImportMP3WDR)
