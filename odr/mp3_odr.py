"""
Max Payne 3 openFormats .odr importer for Blender 4.x

.odr format (Version 144 20):
  { Shaders { shader.sps { DiffuseSampler tex BumpSampler tex ... } }
    Skeleton null
    LodGroup { Center AABBMin AABBMax Radius
      High dist { path\\mesh.mesh shader_index  ... }
      Med dist  Low dist  Vlow dist }
    Light null }

.mesh format (Version 140 21):
  { Locked False  Skinned False  Bounds { ... }
    Geometries { Geometry { ShaderIndex N  Indices N { i0 i1 i2 ... }
      Vertices N { x y z / nx ny nz / r g b a / u v / tx ty tz sign } } } }

Inside the parser, every line becomes (first_token, [remaining_tokens]).
Indices lines:  "0 1 2 2 3 0"  → ('0', ['1','2','2','3','0'])
Vertices lines: "0.31 -1.53 0.67 / 0 0 -1 / 69 69 69 255 / ..."
               → ('0.31', ['-1.53','0.67','/','0','0','-1','/','69','69','69','255','/','...'])
"""

from pathlib import Path
from time import time

import bpy
from bpy.props import StringProperty, CollectionProperty
from bpy.types import Operator, PropertyGroup
from bpy_extras.io_utils import ImportHelper

from ..blender_utils import create_empty_obj, try_unregister_class
from ..material import _create_texture_node, _is_alpha_shader


# ─── Generic block parser ─────────────────────────────────────────────────────

def _parse_block_text(text):
    """
    Parse brace-delimited openFormats text.
    Returns list of (keyword, value) where value is either
    a list-of-tokens (leaf) or a list-of-(key,val) (sub-block).
    Every content line becomes (first_token, [rest_tokens]).
    """
    lines = [l.rstrip('\r\n') for l in text.splitlines()]
    pos = [0]

    def peek():
        while pos[0] < len(lines):
            l = lines[pos[0]].strip()
            if l and not l.startswith('//'): return l
            pos[0] += 1
        return None

    def consume():
        l = peek(); pos[0] += 1; return l

    def parse_block():
        consume()  # opening {
        entries = []
        while True:
            l = peek()
            if l is None or l == '}': consume(); break
            tokens = l.split()
            key = tokens[0]; consume()
            if peek() == '{':
                entries.append((key, parse_block()))
            else:
                entries.append((key, tokens[1:]))
        return entries

    consume()  # "Version X Y"
    return parse_block()


def _as_dict(entries):
    """Convert list of (k,v) to dict. Duplicate keys become lists."""
    d = {}
    for k, v in entries:
        if k not in d:
            d[k] = v
        else:
            existing = d[k]
            if not isinstance(existing, list) or (existing and not isinstance(existing[0], tuple) and not isinstance(existing[0], list)):
                d[k] = [existing, v]
            else:
                d[k].append(v)
    return d


# ─── Indices decoder ──────────────────────────────────────────────────────────

def _decode_indices(idx_block):
    """
    Flatten a parsed Indices block into a list of ints.
    Each entry is (first_int_str, [rest_int_strs]).
    """
    result = []
    for k, v in idx_block:
        try:
            result.append(int(k))
        except ValueError:
            continue
        if isinstance(v, list):
            for x in v:
                try:
                    result.append(int(x))
                except ValueError:
                    break  # hit a non-int (shouldn't happen in valid files)
    return result


# ─── Vertex decoder ───────────────────────────────────────────────────────────

def _decode_vertices(vert_block):
    """
    Decode parsed Vertices block.
    Each entry: (x_str, [y_str, z_str, '/', nx, ny, nz, '/', r, g, b, a, '/', u, v, '/', tx, ty, tz, sign])
    Returns (verts, normals, colors, uvs) — all lists.
    """
    verts, normals, colors, uvs = [], [], [], []
    for k, v in vert_block:
        # Reconstruct full line then split on '/'
        all_tokens = [k] + (v if isinstance(v, list) else [])
        full = ' '.join(all_tokens)
        parts = [p.strip() for p in full.split('/')]
        try:
            p0 = parts[0].split()
            verts.append((float(p0[0]), float(p0[1]), float(p0[2])))
        except (ValueError, IndexError):
            verts.append((0.0, 0.0, 0.0))
        try:
            p1 = parts[1].split()
            normals.append((float(p1[0]), float(p1[1]), float(p1[2])))
        except (ValueError, IndexError):
            normals.append((0.0, 0.0, 1.0))
        try:
            p2 = parts[2].split()
            colors.append((int(p2[0]), int(p2[1]), int(p2[2]), int(p2[3])))
        except (ValueError, IndexError):
            colors.append((255, 255, 255, 255))
        try:
            p3 = parts[3].split()
            uvs.append((float(p3[0]), float(p3[1])))
        except (ValueError, IndexError):
            uvs.append((0.0, 0.0))
    return verts, normals, colors, uvs


# ─── .mesh file parser ────────────────────────────────────────────────────────

def _parse_mesh_file(filepath: Path):
    """
    Parse a MP3 .mesh file.
    Returns list of dicts: {shader_index, faces, verts, normals, colors, uvs}
    """
    text = filepath.read_text(encoding='utf-8', errors='replace')
    entries = _parse_block_text(text)
    root = _as_dict(entries)

    geometries_block = root.get('Geometries', [])
    geom_entries = [(k, v) for k, v in geometries_block if k == 'Geometry']

    result = []
    for _, geom_data in geom_entries:
        gd = _as_dict(geom_data)

        shader_idx = 0
        si = gd.get('ShaderIndex', ['0'])
        try:
            shader_idx = int(si[0] if isinstance(si, list) else si)
        except (ValueError, TypeError):
            pass

        idx_block = gd.get('Indices', [])
        flat_indices = _decode_indices(idx_block) if isinstance(idx_block, list) else []

        vert_block = gd.get('Vertices', [])
        verts, normals, colors, uvs = _decode_vertices(vert_block) if isinstance(vert_block, list) else ([], [], [], [])

        # Build triangles from flat index list (already triangulated in MP3)
        n = len(verts)
        faces = []
        for i in range(0, len(flat_indices) - 2, 3):
            tri = (
                min(flat_indices[i],   n - 1),
                min(flat_indices[i+1], n - 1),
                min(flat_indices[i+2], n - 1),
            )
            faces.append(tri)

        result.append({
            'shader_index': shader_idx,
            'faces': faces,
            'verts': verts,
            'normals': normals,
            'colors': colors,
            'uvs': uvs,
        })

    return result


# ─── Material builder ─────────────────────────────────────────────────────────

def _build_mp3_materials(shaders_block, odr_dir: Path):
    """Build Blender materials from MP3 Shaders block entries."""
    materials = []
    if not isinstance(shaders_block, list):
        return materials

    for shader_name, shader_data in shaders_block:
        params = _as_dict(shader_data) if isinstance(shader_data, list) else {}
        mat_name = Path(shader_name).stem  # strip .sps

        mat = bpy.data.materials.new(name=mat_name)
        mat.use_nodes = True
        nodes = mat.node_tree.nodes
        nodes.clear()

        output = nodes.new("ShaderNodeOutputMaterial")
        output.location = (300, 0)
        bsdf = nodes.new("ShaderNodeBsdfPrincipled")
        bsdf.location = (0, 0)
        mat.node_tree.links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])

        x_off = -300

        # Diffuse
        diff = params.get('DiffuseSampler')
        if diff:
            tex_path = (diff[0] if isinstance(diff, list) else diff).replace('\\', '/')
            tex_node = _create_texture_node(nodes, tex_path, odr_dir, x_off, 200)
            if tex_node:
                mat.node_tree.links.new(tex_node.outputs["Color"], bsdf.inputs["Base Color"])
            x_off -= 300

        # Normal/Bump
        bump_path = params.get('BumpSampler')
        if bump_path:
            tex_path = (bump_path[0] if isinstance(bump_path, list) else bump_path).replace('\\', '/')
            bump_node = _create_texture_node(nodes, tex_path, odr_dir, x_off - 300, -100, is_non_color=True)
            if bump_node:
                nmap = nodes.new("ShaderNodeNormalMap")
                nmap.location = (x_off, -100)
                try:
                    strength = float((params.get('bumpiness') or ['1.0'])[0])
                    nmap.inputs["Strength"].default_value = strength
                except (ValueError, TypeError, IndexError):
                    pass
                mat.node_tree.links.new(bump_node.outputs["Color"], nmap.inputs["Color"])
                mat.node_tree.links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])

        # Specular factor
        spec = params.get('specularFactor')
        if spec:
            try:
                bsdf.inputs["Specular IOR Level"].default_value = min(float(spec[0]) / 100.0, 1.0)
            except (ValueError, TypeError, IndexError):
                pass

        # Alpha blend
        if _is_alpha_shader(mat_name):
            if hasattr(mat, "surface_render_method"):
                mat.surface_render_method = "BLENDED"
            elif hasattr(mat, "blend_method"):
                mat.blend_method = "BLEND"

        materials.append(mat)

    return materials


# ─── Blender mesh builder ─────────────────────────────────────────────────────

def _build_blender_mesh(name, geo, materials, collection):
    """Create and link a Blender mesh object from parsed geometry data."""
    verts   = geo['verts']
    faces   = geo['faces']
    normals = geo['normals']
    colors  = geo['colors']
    uvs     = geo['uvs']

    if not verts:
        return None

    mesh = bpy.data.meshes.new(name)
    obj  = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)

    mesh.from_pydata(verts, [], faces)

    # Vertex colours (BYTE_COLOR, CORNER domain — Blender 4.x safe)
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
            c = colors[vi] if vi < len(colors) else (255, 255, 255, 255)
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

    # Custom normals (Blender 4.1+ via sharp_vector attribute)
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

    # Material slot
    si = geo.get('shader_index', 0)
    if materials and 0 <= si < len(materials):
        obj.data.materials.append(materials[si])

    mesh.validate(verbose=False)
    mesh.update()
    return obj


# ─── Main importer ────────────────────────────────────────────────────────────

def import_mp3_odr(self, filepath: Path) -> int:
    """Import a MP3 .odr file. Returns number of mesh objects created."""
    text = filepath.read_text(encoding='utf-8', errors='replace')
    entries = _parse_block_text(text)
    root = _as_dict(entries)

    odr_dir    = filepath.parent
    filename   = filepath.stem
    collection = bpy.context.collection

    root_empty = create_empty_obj(filepath.name)
    root_empty["filepath"] = str(filepath)

    # Build materials
    shaders_block = root.get('Shaders', [])
    materials = _build_mp3_materials(shaders_block, odr_dir)

    # Find mesh paths from LodGroup → High
    lod_block = root.get('LodGroup', [])
    lod_dict  = _as_dict(lod_block) if isinstance(lod_block, list) else {}
    high_block = lod_dict.get('High', [])

    mesh_paths = []
    if isinstance(high_block, list):
        for k, v in high_block:
            # k = relative mesh path (with backslashes), v = [shader_index]
            rel = k.replace('\\', '/')
            if rel.endswith('.mesh'):
                mesh_paths.append(rel)

    count = 0
    for rel in mesh_paths:
        mesh_filepath = (odr_dir / rel).resolve()
        if not mesh_filepath.exists():
            # Fallback: just the filename in the odr folder
            mesh_filepath = (odr_dir / Path(rel).name).resolve()
        if not mesh_filepath.exists():
            self.report({"WARNING"}, f"Mesh not found: {rel}")
            continue

        try:
            geometries = _parse_mesh_file(mesh_filepath)
        except Exception as e:
            self.report({"WARNING"}, f"Failed to parse {mesh_filepath.name}: {e}")
            continue

        for i, geo in enumerate(geometries):
            name = f"{filename}_{Path(rel).stem}_{i}"
            obj  = _build_blender_mesh(name, geo, materials, collection)
            if obj:
                obj.parent = root_empty
                obj.matrix_parent_inverse = root_empty.matrix_world.inverted()
                count += 1

    return count


# ─── Operator ─────────────────────────────────────────────────────────────────

class ImportMP3ODR(Operator, ImportHelper):
    """Imports Open Drawable Resource"""

    bl_idname  = "mp3_ofio.import_odr"
    bl_label   = "Import .odr"

    filename_ext = ".odr"
    filter_glob: StringProperty(default="*.odr", options={"HIDDEN"})
    files: CollectionProperty(type=PropertyGroup)

    def execute(self, context):
        folder = Path(self.filepath).parent
        count  = 0
        t      = time()
        for sel in self.files:
            fp = folder / sel.name
            try:
                count += import_mp3_odr(self, fp)
            except Exception as e:
                import traceback
                self.report({"ERROR"}, f"{sel.name}: {e}\n{traceback.format_exc()}")
        self.report({"INFO"}, f"Imported {count} mesh(es) in {time()-t:.4f}sec")
        return {"FINISHED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


def register():
    try_unregister_class(ImportMP3ODR)
    bpy.utils.register_class(ImportMP3ODR)


def unregister():
    bpy.utils.unregister_class(ImportMP3ODR)
