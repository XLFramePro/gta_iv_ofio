"""
Max Payne 3 openFormats .odd (Drawable Dictionary) importer for Blender 4.x

Format (Version 144 20):
  Version 144 20
  {
    folder\\drawable_name.odr
    folder\\drawable_name2.odr
    ...
  }

Each line inside {} is a relative path to an .odr file.
"""

from pathlib import Path
from time import time

import bpy
from bpy.props import StringProperty, CollectionProperty
from bpy.types import Operator, PropertyGroup
from bpy_extras.io_utils import ImportHelper

from ..blender_utils import create_empty_obj, try_unregister_class
from ..odr.mp3_odr import import_mp3_odr


# ─── Parser ───────────────────────────────────────────────────────────────────

def _parse_odd(text):
    """Parse a MP3 .odd file and return list of relative .odr paths."""
    odr_paths = []
    lines = text.splitlines()
    inside = False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('//'):
            continue
        if stripped.startswith('Version'):
            continue
        if stripped == '{':
            inside = True
            continue
        if stripped == '}':
            inside = False
            continue
        if inside and stripped:
            # Normalise path separators
            odr_paths.append(stripped.replace('\\', '/'))
    return odr_paths


# ─── Main importer ────────────────────────────────────────────────────────────

def import_mp3_odd(self, filepath: Path) -> tuple:
    """Import a MP3 .odd file. Returns (num_drawables, num_meshes)."""
    text = filepath.read_text(encoding='utf-8', errors='replace')
    odr_paths = _parse_odd(text)

    odd_dir = filepath.parent
    filename = filepath.stem

    root_empty = create_empty_obj(filepath.name)
    root_empty["filepath"] = str(filepath)

    drawables = 0
    total_meshes = 0

    for rel_path in odr_paths:
        odr_filepath = (odd_dir / rel_path).resolve()
        if not odr_filepath.exists():
            self.report({"WARNING"}, f"ODR not found: {rel_path}")
            continue

        try:
            # Create a sub-empty per drawable
            drawable_empty = create_empty_obj(odr_filepath.stem)
            drawable_empty["filepath"] = str(odr_filepath)

            # Import the ODR into a temp empty, then re-parent under drawable_empty
            count = import_mp3_odr(self, odr_filepath)

            # Find the just-created root empty (last created empty with that name)
            odr_root = bpy.data.objects.get(odr_filepath.name)
            if odr_root and odr_root.parent is None:
                odr_root.parent = root_empty
                odr_root.matrix_parent_inverse = root_empty.matrix_world.inverted()

            drawables += 1
            total_meshes += count
        except Exception as e:
            import traceback
            self.report({"WARNING"}, f"Failed to import {rel_path}: {e}")

    return drawables, total_meshes


# ─── Operator ─────────────────────────────────────────────────────────────────

class ImportMP3ODD(Operator, ImportHelper):
    """Imports a Max Payne 3 openFormats .odd (Drawable Dictionary) file"""

    bl_idname = "mp3_ofio.import_odd"
    bl_label = "Import .odd [MP3]"

    filename_ext = ".odd"
    filter_glob: StringProperty(default="*.odd", options={"HIDDEN"})
    files: CollectionProperty(type=PropertyGroup)

    def execute(self, context):
        folder = Path(self.filepath).parent
        total_drawables = 0
        total_meshes = 0
        t = time()
        for sel in self.files:
            fp = folder / sel.name
            try:
                d, m = import_mp3_odd(self, fp)
                total_drawables += d
                total_meshes += m
            except Exception as e:
                import traceback
                self.report({"ERROR"}, f"{sel.name}: {e}\n{traceback.format_exc()}")
        self.report({"INFO"},
            f"Imported {total_drawables} drawable(s), {total_meshes} mesh(es) in {time()-t:.4f}sec")
        return {"FINISHED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


def register():
    try_unregister_class(ImportMP3ODD)
    bpy.utils.register_class(ImportMP3ODD)


def unregister():
    bpy.utils.unregister_class(ImportMP3ODD)
