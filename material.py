from pathlib import Path

import bpy
from loguru import logger


def create_materials(shader_list: list[dict], odr_dir: Path) -> list[bpy.types.Material]:
    materials = []
    for i, shader_data in enumerate(shader_list):
        shader_type = shader_data.get("shader_type", "gta_default")
        mat_name = _get_material_name(shader_data, i)
        mat = bpy.data.materials.new(name=mat_name)
        mat.use_nodes = True
        mat.shader.type = shader_type

        node_tree = mat.node_tree
        nodes = node_tree.nodes
        links = node_tree.links

        nodes.clear()

        output_node = nodes.new("ShaderNodeOutputMaterial")
        output_node.location = (300, 0)

        bsdf = nodes.new("ShaderNodeBsdfPrincipled")
        bsdf.location = (0, 0)
        links.new(bsdf.outputs["BSDF"], output_node.inputs["Surface"])

        _setup_shader_nodes(shader_type, shader_data, node_tree, bsdf, odr_dir)

        # Alpha handling (Blender 4.2+ uses surface_render_method; older used blend_method)
        if _is_alpha_shader(shader_type):
            if hasattr(mat, "surface_render_method"):
                mat.surface_render_method = "BLENDED"
            elif hasattr(mat, "blend_method"):
                mat.blend_method = "BLEND"

        materials.append(mat)

    return materials


def _get_material_name(shader_data: dict, index: int) -> str:
    tex_name = shader_data.get("texturesampler_spTexture")
    if tex_name is not None and isinstance(tex_name, str):
        # Strip path prefix and .dds extension
        name = Path(tex_name).stem if "\\" in str(tex_name) or "/" in str(tex_name) else tex_name
        return name
    return f"Material_{index}"


def _is_alpha_shader(shader_type: str) -> bool:
    return "alpha" in shader_type or "cutout" in shader_type or "glass" in shader_type


def _setup_shader_nodes(
    shader_type: str,
    shader_data: dict,
    node_tree: bpy.types.NodeTree,
    bsdf: bpy.types.ShaderNode,
    odr_dir: Path,
):
    nodes = node_tree.nodes
    links = node_tree.links
    x_offset = -300

    # Diffuse
    diffuse_tex_name = shader_data.get("texturesampler_spTexture")
    if diffuse_tex_name is not None and isinstance(diffuse_tex_name, str):
        tex_node = _create_texture_node(nodes, diffuse_tex_name, odr_dir, x_offset, 300)
        if tex_node is not None:
            links.new(tex_node.outputs["Color"], bsdf.inputs["Base Color"])
            if _is_alpha_shader(shader_type):
                links.new(tex_node.outputs["Alpha"], bsdf.inputs["Alpha"])
            x_offset -= 300

    # Terrain layer textures
    for layer_key in ("texturesampler_layer0_spTexture", "texturesampler_layer1_spTexture",
                      "texturesampler_layer2_spTexture", "texturesampler_layer3_spTexture"):
        layer_tex = shader_data.get(layer_key)
        if layer_tex is not None and isinstance(layer_tex, str):
            _create_texture_node(nodes, layer_tex, odr_dir, x_offset, -200)
            x_offset -= 300

    # Normal
    bump_tex_name = shader_data.get("bumpsampler_spTexture")
    bumpiness = shader_data.get("bumpiness_spFloat", 1.0)
    if isinstance(bumpiness, list):
        bumpiness = bumpiness[0] if bumpiness else 1.0

    if bump_tex_name is not None and isinstance(bump_tex_name, str):
        bump_tex_node = _create_texture_node(nodes, bump_tex_name, odr_dir, x_offset - 300, -100, is_non_color=True)
        if bump_tex_node is not None:
            normal_map_node = nodes.new("ShaderNodeNormalMap")
            normal_map_node.location = (x_offset, -100)
            normal_map_node.inputs["Strength"].default_value = float(bumpiness) if bumpiness else 1.0
            links.new(bump_tex_node.outputs["Color"], normal_map_node.inputs["Color"])
            links.new(normal_map_node.outputs["Normal"], bsdf.inputs["Normal"])
            x_offset -= 600

    # Specular
    spec_tex_name = shader_data.get("specsampler_spTexture")
    if spec_tex_name is not None and isinstance(spec_tex_name, str):
        spec_tex_node = _create_texture_node(nodes, spec_tex_name, odr_dir, x_offset - 300, -400, is_non_color=True)
        if spec_tex_node is not None:
            specmask = shader_data.get("specmapintmask_spVector4")
            if specmask and isinstance(specmask, list) and len(specmask) >= 3:
                # Use dot product with mask to extract the correct channel
                dot_node = nodes.new("ShaderNodeVectorMath")
                dot_node.operation = "DOT_PRODUCT"
                dot_node.location = (x_offset, -400)
                dot_node.inputs[1].default_value = (
                    float(specmask[0]), float(specmask[1]), float(specmask[2])
                )
                links.new(spec_tex_node.outputs["Color"], dot_node.inputs[0])
                links.new(dot_node.outputs["Value"], bsdf.inputs["Specular IOR Level"])
            else:
                links.new(spec_tex_node.outputs["Color"], bsdf.inputs["Specular IOR Level"])

    # Specular factor
    spec_factor = shader_data.get("specularfactor_spFloat")
    if spec_factor is not None and spec_tex_name is None:
        # Map specular factor: game range ~0-100 to Blender ~0-1
        try:
            bsdf.inputs["Specular IOR Level"].default_value = float(spec_factor) / 100.0
        except (ValueError, TypeError):
            pass

    # Emissive
    emissive_mult = shader_data.get("emissivemultiplier_spFloat")
    if emissive_mult is not None and "emissive" in shader_type:
        try:
            bsdf.inputs["Emission Strength"].default_value = float(emissive_mult)
            # Connect diffuse to emission color too
            diffuse_tex_name = shader_data.get("texturesampler_spTexture")
            if diffuse_tex_name is not None:
                # Find the existing diffuse texture node
                for node in nodes:
                    if node.type == "TEX_IMAGE" and node.label == _tex_label(diffuse_tex_name):
                        links.new(node.outputs["Color"], bsdf.inputs["Emission Color"])
                        break
        except (ValueError, TypeError):
            pass

    # Reflection
    reflective_power = shader_data.get("reflectivepower_spFloat")
    if reflective_power is not None:
        try:
            # Lower metallic = less reflection, map reflective power to metallic
            bsdf.inputs["Metallic"].default_value = min(float(reflective_power), 1.0)
        except (ValueError, TypeError):
            pass

    # Environment
    env_tex_name = shader_data.get("environmentsampler_spTexture")
    if env_tex_name is not None and isinstance(env_tex_name, str):
        _create_texture_node(nodes, env_tex_name, odr_dir, x_offset - 300, -700, is_non_color=True)

    # Wire shader
    if shader_type == "gta_wire":
        fade = shader_data.get("fade_thickness_spFloat")
        if fade is not None:
            try:
                bsdf.inputs["Alpha"].default_value = float(fade)
            except (ValueError, TypeError):
                pass

    # Dirt
    dirt_tex_name = shader_data.get("dirtsampler_spTexture")
    if dirt_tex_name is not None and isinstance(dirt_tex_name, str):
        _create_texture_node(nodes, dirt_tex_name, odr_dir, x_offset - 300, -550)


def _tex_label(tex_name: str) -> str:
    """Generate a label from a texture name."""
    name = tex_name
    if "\\" in name or "/" in name:
        name = Path(name).stem
    if name.lower().endswith(".dds"):
        name = name[:-4]
    return name


def _create_texture_node(
    nodes, tex_name: str, odr_dir: Path, x: float, y: float, is_non_color: bool = False
) -> bpy.types.ShaderNode | None:
    label = _tex_label(tex_name)
    tex_node = nodes.new("ShaderNodeTexImage")
    tex_node.location = (x, y)
    tex_node.label = label

    image = _find_and_load_texture(tex_name, odr_dir)
    if image is not None:
        tex_node.image = image
        if is_non_color:
            image.colorspace_settings.name = "Non-Color"
    else:
        logger.warning(f"Texture not found: {tex_name}")

    return tex_node


def _find_and_load_texture(tex_name: str, odr_dir: Path) -> bpy.types.Image | None:
    """Search for a texture file and load it into Blender."""
    name = tex_name.replace("\\", "/")
    candidates = []

    # If it already has a path (e.g., "nj2_sw01_01b/file.dds")
    if "/" in name:
        candidates.append(odr_dir / name)

    base_name = Path(name).name if "/" in name else name

    if not base_name.lower().endswith(".dds"):
        base_name_dds = base_name + ".dds"
    else:
        base_name_dds = base_name
        base_name = base_name[:-4]

    # Search directories
    search_dirs = [odr_dir]
    if odr_dir.is_dir():
        for child in odr_dir.iterdir():
            if child.is_dir():
                search_dirs.append(child)

    for search_dir in search_dirs:
        candidates.append(search_dir / base_name_dds)
        candidates.append(search_dir / base_name_dds.lower())

    # Check if already loaded in Blender
    for img in bpy.data.images:
        img_name = Path(img.filepath).stem.lower() if img.filepath else img.name.lower()
        if img_name == base_name.lower():
            return img

    for path in candidates:
        if path.exists():
            try:
                return bpy.data.images.load(str(path))
            except RuntimeError:
                continue

    # Case insensitive search
    target_lower = base_name_dds.lower()
    for search_dir in search_dirs:
        if not search_dir.is_dir():
            continue
        for file in search_dir.iterdir():
            if file.name.lower() == target_lower:
                try:
                    return bpy.data.images.load(str(file))
                except RuntimeError:
                    continue

    return None
