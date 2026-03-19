"""
Max Payne 3 binary .wdr importer for Blender 4.x  (RSC05 / RAGE)

RSC05 layout (after zlib decompression from offset 12):
  [0 .. SYS_SIZE-1]  System segment  – CPU structs, vertex data, texture metadata
  [SYS_SIZE .. end]  Graphics segment – index buffers, DXT pixel data

Virtual pointer encoding:
  0x50xxxxxx  →  sys  offset  =  ptr & 0x00FFFFFF
  0x60xxxxxx  →  gfx  offset  =  SYS_SIZE + (ptr & 0x00FFFFFF)

Key structures parsed:
  CrmDrawable  (root at offset 0)
    [0x08]  shader_group_ptr  →  CrmShaderGroup
    [0x40]  models_coll_ptr   →  collection of CrmModel ptrs

  CrmShaderGroup  (at sg_off)
    [0x04]  shaders_pgArray_ptr
    [0x08]  (unused by this importer)
    [0x0C]  shader_count (u16 lo)
    [0x20 .. +4*N]  N inline shader ptrs
    [0x20 - 0x10]   shader_index_pairs array (u16 pairs, 2 per u32)

  grmShader  (per shader)
    [0x00]  params_ptr   →  param block (12 bytes each)
    [0x08]  param_count  (lower byte)
    Param entry (12 bytes):  hash(4) | flags(4) | data_ptr(4)
      data_ptr pointing to a grmTexture (vftable=0x00836FBC) = texture sampler

  grmTexture  (stride 0x60, vftable=0x00836FBC)
    [0x18]  name_ptr  (0x50 → ASCII string)
    [0x20]  (w<<16)|h  (u32)
    [0x24]  (mips<<16)  (u32)
    [0x28]  DXT format  FourCC  (DXT1/DXT3/DXT5)
    [0x50]  pixel_data_ptr  (0x60 → gfx segment)

  CrmModel  (per geometry, 0x50 bytes)
    [0x0C]  vbd_ptr  →  CrmVertexBuffer
    [0x1C]  ibd_ptr  →  CrmIndexBuffer

  CrmVertexBuffer
    [0x04]  stride (lower 16 bits)
    [0x08]  vertex_data_ptr  (0x50)
    [0x0C]  vertex_count

  CrmIndexBuffer
    [0x04]  index_count
    [0x08]  index_data_ptr  (0x60)

Vertex format  stride=52:  pos(12) + norm(12) + color(4) + uv(8) + tangent(16)
Vertex format  stride=36:  pos(12) + norm(12) + color(4) + uv(8)
Indices: u16 triangles
"""

from pathlib import Path
import struct
import tempfile
import zlib
from time import time

import bpy
from bpy.props import StringProperty, CollectionProperty
from bpy.types import Operator, PropertyGroup
from bpy_extras.io_utils import ImportHelper

from ..blender_utils import create_empty_obj, try_unregister_class


# ─── Constants ────────────────────────────────────────────────────────────────

RSC05_MAGIC       = b"RSC\x05"
RSC05_VERSION     = 144
GRMTEX_VFTABLE    = 0x00836FBC
DXT1              = 0x31545844
DXT3              = 0x33545844
DXT5              = 0x35545844
SUPPORTED_FORMATS = (DXT1, DXT3, DXT5)


# ─── Low-level helpers ────────────────────────────────────────────────────────

def _rp(d, o):  return struct.unpack_from("<I", d, o)[0] if o + 4 <= len(d) else 0
def _ru16(d, o): return struct.unpack_from("<H", d, o)[0] if o + 2 <= len(d) else 0

def _read_str(data, off, maxlen=128):
    end = off
    while end < off + maxlen and end < len(data) and data[end] != 0:
        end += 1
    return data[off:end].decode("utf-8", errors="replace")


# ─── RSC05 container ──────────────────────────────────────────────────────────

class _RSC05:
    """Decompressed RSC05 resource with pointer resolution."""


    def __init__(self, raw: bytes):
        if raw[:4] != RSC05_MAGIC:
            raise ValueError(f"Not an RSC05 file (magic={raw[:4]})")
        version = struct.unpack_from("<I", raw, 4)[0]
        if version != RSC05_VERSION:
            raise ValueError(f"Unsupported RSC05 version {version} (expected {RSC05_VERSION})")
        self._data = zlib.decompress(raw[12:])
        self.sys_size = self._probe_sys_size()

    # ── pointer resolution ──
    def resolve(self, ptr):
        if ptr == 0 or ptr == 0xFFFFFFFF:
            return None
        hi = ptr >> 24
        lo = ptr & 0x00FFFFFF
        if hi == 0x50: return lo
        if hi == 0x60: return self.sys_size + lo
        return None

    def rp(self, off):   return _rp(self._data, off)
    def ru16(self, off): return _ru16(self._data, off)
    def rs(self, off):   return _read_str(self._data, off)
    def raw_slice(self, off, n): return self._data[off:off + n]

    # ── sys_size probe ──
    def _probe_sys_size(self):
        data = self._data
        # Find model0 → VBD → IBD → gfx ptr, then scan sys_size candidates
        models_ptr   = _rp(data, 0x40)
        coll_off     = models_ptr & 0x00FFFFFF if (models_ptr >> 24) == 0x50 else None
        if not coll_off:
            return 0x180000
        model0_ptr = _rp(data, coll_off + 0x50)
        model0_off = model0_ptr & 0x00FFFFFF if (model0_ptr >> 24) == 0x50 else None
        if not model0_off:
            return 0x180000
        vbd_ptr  = _rp(data, model0_off + 0x0C)
        ibd_ptr  = _rp(data, model0_off + 0x1C)
        vbd_off  = vbd_ptr & 0x00FFFFFF if (vbd_ptr >> 24) == 0x50 else None
        ibd_off  = ibd_ptr & 0x00FFFFFF if (ibd_ptr >> 24) == 0x50 else None
        if not (vbd_off and ibd_off):
            return 0x180000
        vcount    = _rp(data, vbd_off + 0x0C)
        icount    = _rp(data, ibd_off + 0x04)
        idata_ptr = _rp(data, ibd_off + 0x08)
        if (idata_ptr >> 24) != 0x60:
            return 0x180000
        ib_gfx = idata_ptr & 0x00FFFFFF
        for sys_sz in range(0x100000, 0x400000, 0x20000):
            phys = sys_sz + ib_gfx
            if phys + icount * 2 > len(data):
                continue
            idxs = struct.unpack_from(f"<{icount}H", data, phys)
            if max(idxs) < vcount:
                return sys_sz
        return 0x180000


# ─── Texture extraction ───────────────────────────────────────────────────────

def _dxt_mip_size(w, h, fmt):
    bs = 8 if fmt == DXT1 else 16
    return max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * bs

def _make_dds_header(w, h, mips, fmt):
    DDSD_CAPS = 0x1; DDSD_HEIGHT = 0x2; DDSD_WIDTH = 0x4
    DDSD_PIXELFORMAT = 0x1000; DDSD_LINEARSIZE = 0x80000; DDSD_MIPMAPCOUNT = 0x20000
    DDPF_FOURCC = 0x4
    DDSCAPS_TEXTURE = 0x1000; DDSCAPS_MIPMAP = 0x400000; DDSCAPS_COMPLEX = 0x8

    flags = DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT | DDSD_LINEARSIZE
    caps  = DDSCAPS_TEXTURE
    if mips > 1:
        flags |= DDSD_MIPMAPCOUNT
        caps  |= DDSCAPS_MIPMAP | DDSCAPS_COMPLEX

    pitch = _dxt_mip_size(w, h, fmt)
    header  = struct.pack("<4sIIIIII", b"DDS ", 124, flags, h, w, pitch, 0)
    header += struct.pack("<I", max(1, mips))
    header += b"\x00" * 44
    header += struct.pack("<II4sIIIII", 32, DDPF_FOURCC, fmt.to_bytes(4, "little"), 0,0,0,0,0)
    header += struct.pack("<IIIII", caps, 0, 0, 0, 0)
    return header  # 128 bytes

def _extract_textures(rsc: _RSC05) -> dict:
    """
    Scan sys segment for all grmTexture structs.
    Returns dict: tex_name → bpy.types.Image
    """
    data     = rsc._data
    sys_size = rsc.sys_size
    result   = {}

    tmp_dir = tempfile.mkdtemp(prefix="wdr_tex_")

    for off in range(0, sys_size - 0x60, 4):
        if _rp(data, off) != GRMTEX_VFTABLE:
            continue
        fmt = _rp(data, off + 0x28)
        if fmt not in SUPPORTED_FORMATS:
            continue

        name_ptr = _rp(data, off + 0x18)
        name_off = rsc.resolve(name_ptr)
        w_raw    = _rp(data, off + 0x20)
        w        = (w_raw >> 16) & 0xFFFF
        h        = w_raw & 0xFFFF
        mips     = max(1, (_rp(data, off + 0x24) >> 16) & 0xFF)
        pix_ptr  = _rp(data, off + 0x50)
        pix_off  = rsc.resolve(pix_ptr)

        if not (name_off and pix_off and w > 0 and h > 0):
            continue
        if pix_off + 16 > len(data):
            continue

        raw_name = _read_str(data, name_off, 48)
        # Clean: take only the first name (sometimes two are concatenated)
        tex_name = raw_name.split("\x00")[0].strip()
        if not tex_name or tex_name in result:
            continue

        # Compute total pixel data size
        total_pix = sum(
            _dxt_mip_size(max(1, w >> m), max(1, h >> m), fmt)
            for m in range(mips)
        )
        if pix_off + total_pix > len(data):
            total_pix = len(data) - pix_off
        if total_pix <= 0:
            continue

        # Write DDS to temp file
        dds_path = str(Path(tmp_dir) / f"{tex_name}.dds")
        dds_data = _make_dds_header(w, h, mips, fmt) + data[pix_off:pix_off + total_pix]
        with open(dds_path, "wb") as f:
            f.write(dds_data)

        # Load into Blender
        try:
            img = bpy.data.images.load(dds_path)
            img.name = tex_name
            # Color space: _nm / _n = Non-Color, else sRGB
            suffix = tex_name.lower()
            is_normal = suffix.endswith("_nm") or suffix.endswith("_n")
            img.colorspace_settings.name = "Non-Color" if is_normal else "sRGB"
            result[tex_name] = img
        except Exception:
            pass

    return result


# ─── Shader → texture mapping ─────────────────────────────────────────────────

def _parse_shader_group(rsc: _RSC05) -> tuple:
    """
    Returns:
      shader_textures : dict[shader_idx] → [tex_name, ...]   (first = diffuse)
      geom_shader_idx : list[int]  (per-geometry shader index, 16 entries)
    """
    sg_ptr = rsc.rp(0x08)
    sg_off = rsc.resolve(sg_ptr)
    if sg_off is None:
        return {}, []

    shader_count = rsc.ru16(sg_off + 0x0C)

    # ── per-shader texture names ──
    shader_textures = {}
    for si in range(shader_count):
        shader_ptr = rsc.rp(sg_off + 0x20 + si * 4)
        shader_off = rsc.resolve(shader_ptr)
        if shader_off is None:
            continue
        params_ptr  = rsc.rp(shader_off + 0x00)
        param_count = rsc.rp(shader_off + 0x08) & 0xFF
        params_off  = rsc.resolve(params_ptr)
        if params_off is None:
            continue
        tex_names = []
        for pi in range(param_count):
            p_off  = params_off + pi * 12
            p_ptr  = rsc.rp(p_off + 8)
            p_off2 = rsc.resolve(p_ptr)
            if p_off2 is None:
                continue
            if rsc.rp(p_off2) == GRMTEX_VFTABLE:
                name_ptr = rsc.rp(p_off2 + 0x18)
                name_off = rsc.resolve(name_ptr)
                if name_off:
                    raw = _read_str(rsc._data, name_off, 48)
                    tex_names.append(raw.split("\x00")[0].strip())
        shader_textures[si] = tex_names

    # ── per-geometry shader index array ──
    # Located just before the inline shader ptr list, at sg_off+0x10
    # Packed as u32 where lo16=geom_even, hi16=geom_odd
    geom_shader_idx = []
    geom_idx_arr_off = sg_off + 0x10
    for i in range(8):
        v    = rsc.rp(geom_idx_arr_off + i * 4)
        lo16 = v & 0xFFFF
        hi16 = (v >> 16) & 0xFFFF
        geom_shader_idx.extend([lo16, hi16])

    return shader_textures, geom_shader_idx


# ─── Material builder ─────────────────────────────────────────────────────────

def _make_material(name: str, tex_images: list) -> bpy.types.Material:
    """Create a Principled BSDF material from a list of images (diffuse first)."""
    mat = bpy.data.materials.get(name)
    if mat:
        return mat

    mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    nodes.clear()

    output = nodes.new("ShaderNodeOutputMaterial"); output.location = (400, 0)
    bsdf   = nodes.new("ShaderNodeBsdfPrincipled"); bsdf.location   = (0, 0)
    mat.node_tree.links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])

    x = -300
    for img in tex_images:
        if img is None:
            continue
        tex_node = nodes.new("ShaderNodeTexImage")
        tex_node.image    = img
        tex_node.location = (x, 200 if x == -300 else -150)

        name_lower = img.name.lower()
        is_normal  = name_lower.endswith("_nm") or name_lower.endswith("_n")

        if is_normal:
            nmap = nodes.new("ShaderNodeNormalMap")
            nmap.location = (-100, -150)
            mat.node_tree.links.new(tex_node.outputs["Color"], nmap.inputs["Color"])
            mat.node_tree.links.new(nmap.outputs["Normal"],    bsdf.inputs["Normal"])
        else:
            mat.node_tree.links.new(tex_node.outputs["Color"], bsdf.inputs["Base Color"])

        x -= 300

    return mat


# ─── Geometry reader ──────────────────────────────────────────────────────────

def _read_geometry(rsc: _RSC05, model_off: int) -> dict | None:
    vbd_ptr = rsc.rp(model_off + 0x0C)
    ibd_ptr = rsc.rp(model_off + 0x1C)
    vbd_off = rsc.resolve(vbd_ptr)
    ibd_off = rsc.resolve(ibd_ptr)
    if not (vbd_off and ibd_off):
        return None

    stride    = rsc.rp(vbd_off + 0x04) & 0xFFFF
    vcount    = rsc.rp(vbd_off + 0x0C)
    vdata_ptr = rsc.rp(vbd_off + 0x08)
    icount    = rsc.rp(ibd_off + 0x04)
    idata_ptr = rsc.rp(ibd_off + 0x08)

    vdata_off = rsc.resolve(vdata_ptr)
    idata_off = rsc.resolve(idata_ptr)
    if not (vdata_off and idata_off and stride and vcount and icount):
        return None
    if icount % 3 != 0:
        return None

    data = rsc._data
    verts, normals, colors, uvs = [], [], [], []
    for i in range(vcount):
        off = vdata_off + i * stride
        if off + 28 > len(data): break
        x, y, z    = struct.unpack_from("<fff", data, off)
        nx, ny, nz = struct.unpack_from("<fff", data, off + 12)
        r, g, b, a = data[off+24], data[off+25], data[off+26], data[off+27]
        u = v = 0.0
        if stride >= 36 and off + 36 <= len(data):
            u, v = struct.unpack_from("<ff", data, off + 28)
        verts.append((x, y, z))
        normals.append((nx, ny, nz))
        colors.append((r, g, b, a))
        uvs.append((u, v))

    if not verts:
        return None

    n = len(verts)
    faces = []
    if idata_off + icount * 2 <= len(data):
        raw_idx = struct.unpack_from(f"<{icount}H", data, idata_off)
        for t in range(0, icount - 2, 3):
            i0, i1, i2 = raw_idx[t], raw_idx[t+1], raw_idx[t+2]
            if i0 < n and i1 < n and i2 < n:
                faces.append((i0, i1, i2))

    return {"verts": verts, "normals": normals, "colors": colors,
            "uvs": uvs, "faces": faces} if faces else None


# ─── Blender mesh builder ─────────────────────────────────────────────────────

def _build_mesh(name: str, geo: dict, mat: bpy.types.Material | None,
                collection) -> bpy.types.Object | None:
    mesh = bpy.data.meshes.new(name)
    obj  = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    mesh.from_pydata(geo["verts"], [], geo["faces"])

    # Vertex colours
    colors = geo["colors"]
    if colors:
        CNAME = "Col"
        if CNAME in mesh.attributes:
            ex = mesh.attributes[CNAME]
            if ex.data_type != "BYTE_COLOR" or ex.domain != "CORNER":
                mesh.attributes.remove(ex)
        if CNAME not in mesh.attributes:
            mesh.attributes.new(CNAME, "BYTE_COLOR", "CORNER")
        ca   = mesh.attributes[CNAME]
        flat = []
        for loop in mesh.loops:
            c = colors[loop.vertex_index]
            flat.extend(x / 255.0 for x in c)
        ca.data.foreach_set("color", flat)
        try:
            mesh.color_attributes.active_color = mesh.color_attributes[CNAME]
        except Exception:
            pass

    # UVs
    uvs = geo["uvs"]
    if uvs:
        uv_layer = mesh.uv_layers.new(name="UVMap").uv
        for loop in mesh.loops:
            vi = loop.vertex_index
            if vi < len(uvs):
                u, v = uvs[vi]
                uv_layer[loop.index].vector = (u, 1.0 - v)

    # Custom normals
    normals = geo["normals"]
    if normals and len(normals) == len(geo["verts"]):
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

    # Material
    if mat:
        obj.data.materials.append(mat)

    mesh.validate(verbose=False)
    mesh.update()
    return obj


# ─── Main importer ────────────────────────────────────────────────────────────

def import_mp3_wdr(self, filepath: Path) -> int:
    raw  = filepath.read_bytes()
    rsc  = _RSC05(raw)
    coll = bpy.context.collection

    # 1. Extract all embedded textures → Blender images
    tex_images = _extract_textures(rsc)

    # 2. Parse shader group → per-shader tex names + per-geom shader index
    shader_textures, geom_shader_idx = _parse_shader_group(rsc)

    # 3. Build materials (one per shader that has textures)
    materials = {}
    for si, tex_names in shader_textures.items():
        if not tex_names:
            continue
        imgs = [tex_images.get(n) for n in tex_names if tex_images.get(n)]
        if imgs:
            mat_name = tex_names[0]  # use diffuse name as material name
            materials[si] = _make_material(mat_name, imgs)

    # 4. Collect model offsets
    models_ptr     = rsc.rp(0x40)
    collection_off = models_ptr & 0x00FFFFFF if (models_ptr >> 24) == 0x50 else None
    if collection_off is None:
        raise ValueError("Could not find models collection")

    model_offs = []
    model_ptrs_off = collection_off + 0x50
    for i in range(64):
        mp  = rsc.rp(model_ptrs_off + i * 4)
        hi  = mp >> 24
        if hi not in (0x50, 0x60):
            break
        off = rsc.resolve(mp)
        if off:
            model_offs.append(off)

    if not model_offs:
        raise ValueError("No geometry models found in WDR")

    # 5. Create root empty
    root_empty = create_empty_obj(filepath.name)
    root_empty["filepath"] = str(filepath)

    # 6. Import each geometry with its material
    count = 0
    for gi, model_off in enumerate(model_offs):
        geo = _read_geometry(rsc, model_off)
        if geo is None:
            continue

        si  = geom_shader_idx[gi] if gi < len(geom_shader_idx) else 0
        mat = materials.get(si)

        name = f"{filepath.stem}_geo{gi}"
        obj  = _build_mesh(name, geo, mat, coll)
        if obj:
            obj.parent = root_empty
            obj.matrix_parent_inverse = root_empty.matrix_world.inverted()
            count += 1

    return count


# ─── Operator ─────────────────────────────────────────────────────────────────

class ImportMP3WDR(Operator, ImportHelper):
    """Imports Binary Drawable Resource"""

    bl_idname    = "mp3_ofio.import_wdr"
    bl_label     = "Import .wdr"
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
