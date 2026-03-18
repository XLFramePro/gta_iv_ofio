import bpy
from bpy.types import Menu, TOPBAR_MT_file_import, TOPBAR_MT_file_export
from loguru import logger

from .blender_utils import try_unregister_class
from .export_light import register as register_export_light, unregister as unregister_export_light, EXPORT_OT_IV_Lights
from .export_mesh import register as register_export_mesh, unregister as unregister_export_mesh, EXPORT_OT_IV_Mesh
from .light import register as register_import_light, unregister as unregister_import_light, ImportGTAIVLight
from .mesh import register as register_import_mesh, unregister as unregister_import_mesh, ImportGTAIVMesh
from .odd.gtaiv_odd import register as register_import_odd, unregister as unregister_import_odd, ImportGTAIVODD
from .wdr.import_mp3_wdr import register as register_mp3_wdr, unregister as unregister_mp3_wdr, ImportMP3WDR
from .odr.gtaiv_odr import register as register_import_odr, unregister as unregister_import_odr, ImportGTAIVODR
from .oft.gtaiv_oft import register as register_import_oft, unregister as unregister_import_oft, ImportGTAIVOFT
from .obn.gtaiv_obn import register as register_import_obn, unregister as unregister_import_obn, ImportGTAIVOBN
from .odr.mp3_odr import register as register_mp3_odr, unregister as unregister_mp3_odr, ImportMP3ODR
from .odd.mp3_odd import register as register_mp3_odd, unregister as unregister_mp3_odd, ImportMP3ODD
from .ofio_ui import register as register_ui, unregister as unregister_ui
from .panels import register as register_panels, unregister as unregister_panels
from .properties import register as register_properties, unregister as unregister_properties

file_logger_id = None


class GTA4_MT_import_menu(Menu):
    bl_idname = "GTA4_MT_Import_Menu"
    bl_label = "GTA IV / Max Payne 3 openFormats"

    def draw(self, context):
        layout = self.layout
        layout.operator(ImportGTAIVODR.bl_idname)
        layout.operator(ImportGTAIVOFT.bl_idname)
        layout.operator(ImportGTAIVODD.bl_idname)
        layout.operator(ImportGTAIVOBN.bl_idname)

        layout.separator()

        layout.operator(ImportGTAIVMesh.bl_idname)
        layout.operator(ImportGTAIVLight.bl_idname)

        layout.separator()

        layout.operator(ImportMP3ODR.bl_idname)
        layout.operator(ImportMP3ODD.bl_idname)
        layout.operator(ImportMP3WDR.bl_idname)
        

def import_menu_draw(self, context):
    self.layout.menu(GTA4_MT_import_menu.bl_idname)



class GTA4_MT_Export_Menu(Menu):
    bl_idname = "GTA4_MT_Export_Menu"
    bl_label = "GTA IV openFormats"

    def draw(self, context):
        layout = self.layout
        layout.operator(EXPORT_OT_IV_Lights.bl_idname)
        layout.operator(EXPORT_OT_IV_Mesh.bl_idname)


def export_menu_draw(self, context):
    self.layout.menu(GTA4_MT_Export_Menu.bl_idname)


def register():
    global file_logger_id
    extension_directory = bpy.utils.extension_path_user(__package__)
    import os
    _log_path = os.path.join(extension_directory, "gta4-ofio.log")
    file_logger_id = logger.add(_log_path, rotation="1 day", retention="3 days")
    logger.info(f"GTA4 Blender Addon log file location: {extension_directory}")

    # Properties & Panels
    register_properties()
    register_panels()
    register_ui()

    # Import operators
    register_import_odr()
    register_import_oft()
    register_import_odd()
    register_import_obn()
    register_import_mesh()
    register_import_light()

    # MP3 Import operators
    register_mp3_odr()
    register_mp3_odd()
    register_mp3_wdr()

    # Import menu
    try_unregister_class(GTA4_MT_import_menu)
    bpy.utils.register_class(GTA4_MT_import_menu)
    TOPBAR_MT_file_import.append(import_menu_draw)

    # Export operators
    register_export_light()
    register_export_mesh()

    # Export menu
    try_unregister_class(GTA4_MT_Export_Menu)
    bpy.utils.register_class(GTA4_MT_Export_Menu)
    TOPBAR_MT_file_export.append(export_menu_draw)


def unregister():
    logger.remove(file_logger_id)

    # UI & Panels
    unregister_ui()
    unregister_panels()
    unregister_properties()

    # MP3 Import
    unregister_mp3_odr()
    unregister_mp3_odd()
    unregister_mp3_wdr()

    # Import
    unregister_import_odr()
    unregister_import_oft()
    unregister_import_odd()
    unregister_import_obn()
    unregister_import_mesh()
    unregister_import_light()

    bpy.utils.unregister_class(GTA4_MT_import_menu)
    TOPBAR_MT_file_import.remove(import_menu_draw)

    # Export
    unregister_export_light()
    unregister_export_mesh()

    bpy.utils.unregister_class(GTA4_MT_Export_Menu)
    TOPBAR_MT_file_export.remove(export_menu_draw)
