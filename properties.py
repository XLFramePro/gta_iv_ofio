import bpy
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
    FloatProperty,
)
from bpy.types import (
    Material,
    Mesh,
    Bone,
    PropertyGroup,
)
import bpy.types as _btypes
# Blender 4.x unified PointLight/SpotLight into a single Light type.
# We keep a single reference so the properties work for both light types.
_Light = getattr(_btypes, "Light", None)
_PointLight = getattr(_btypes, "PointLight", _Light)
_SpotLight = getattr(_btypes, "SpotLight", _Light)
from bpy.utils import register_class, unregister_class

from .blender_utils import try_unregister_class, calculate_spot_blend
from .openformats2json.iv_shaders import shaders


class Mtl(bpy.types.PropertyGroup):
    index: IntProperty(name="Material Index", min=0)


class GTAIVBone(PropertyGroup):
    index: IntProperty()
    id: IntProperty()
    orient: FloatVectorProperty()
    sorient: FloatVectorProperty()


def set_max_value(self, new_value):
    if new_value > bpy.context.object.data.spot_size:
        new_value = bpy.context.object.data.spot_size
    self["InnerConeAngle"] = new_value


def get_max_value(self):
    return self.get("InnerConeAngle", 0)


def update_spot_blend(self, context):
    try:
        context.light.spot_blend = calculate_spot_blend(self.inner_cone_angle, context.light.spot_size)
    except AttributeError:
        ...  # this function gets activated when user imports light, causing `AttributeError: 'Context' object has no attribute 'light'`


def update_power(self, context):
    try:
        context.light.energy = self.intensity * 500
    except AttributeError:
        ...  # this function gets activated when user imports light, causing `AttributeError: 'Context' object has no attribute 'light'`


def update_range(self, context):
    try:
        match context.light.type:
            case "POINT":
                context.light.shadow_soft_size = self.range
            case _:
                context.light.use_custom_distance = True
                context.light.cutoff_distance = self.range
    except AttributeError:
        ...


def max_inner_cone_angle(context) -> float:
    return context.light.spot_size


def update_show_cone(context, volumetric_flag):
    try:
        context.light.show_cone = volumetric_flag
    except AttributeError:
        ...


class LightAttributes(PropertyGroup):
    range: FloatProperty(
        name="Range",
        description="Distance in openFormats",
        min=0.0,
        subtype="DISTANCE",
        update=update_range,
    )
    intensity: FloatProperty(
        name="Intensity",
        description="Range in openFormats",
        min=0.0,
        update=update_power,
    )
    inner_cone_angle: FloatProperty(
        name="InnerConeAngle",
        description="Hotspot in openFormats",
        subtype="ANGLE",
        max=3.14159,
        min=0.0,
        update=update_spot_blend,
        get=lambda self: get_max_value(self),
        set=lambda self, value: set_max_value(self, value),
    )
    vol_intensity: FloatProperty(name="Volume Intensity", description="_f2C", soft_min=0, soft_max=1, step=1)
    vol_size: FloatProperty(name="Volume Size", description="_f30", soft_min=0, soft_max=1, step=1)
    vol_softness: FloatProperty(
        name="Volume Softness",
        description="_f28, thanks to RecklessGlue",
        soft_min=1,
        soft_max=200,
        step=50,
    )
    light_fade_dist: FloatProperty(
        name="Light Fade Distance",
        description="_f60, thanks to RecklessGlue",
        subtype="DISTANCE",
        soft_min=1,
        step=50,
    )
    corona_hash: StringProperty(
        name="Corona Hash",
        description=(
            "Jenkins one-at-a-time hash of name of texture you want to use for corona.\n"
            "That texture should be in corona.wtd.\n"
            "Just fill texture name, addon will create hash for you on export"
        ),
    )
    corona_size: FloatProperty(name="Corona Size", soft_min=0, soft_max=1, step=1)
    corona_intensity: FloatProperty(
        name="Corona Intensity",
        description="_f58, Intensity/Opacity of corona texture",
        soft_min=0,
        soft_max=1,
        step=1,
    )
    luminescence_hash: StringProperty(
        name="Luminescence Hash",
        description=(
            "Jenkins one-at-a-time hash of name of light intensity mask/cookie texture.\n"
            "Texture needs to be in the model's texture dictionary\n"
            "Thanks AssaultKifle47"
        ),
    )
    f5C: StringProperty(name="f5C")
    bone_id: StringProperty(
        name="Bone ID",
    )
    f66: IntProperty(name="f66")


class LightFlags(PropertyGroup):
    updating: BoolProperty(default=False)

    flag: StringProperty(
        name="Flag",
        default="96",
        update=lambda self, context: self.update_flag(context),
    )

    time_flag: EnumProperty(
        name="Time flag",
        description="Control what part of the day the light is on",
        items=[
            ("day", "Day Only", ""),
            ("night", "Night Only", ""),
            ("always_on", "Always On", ""),
        ],
        default="night",
        update=lambda self, context: self.update_optional_flags(context),
    )

    weak_light: BoolProperty(
        name="Weak Light",
        update=lambda self, context: self.update_optional_flags(context),
    )

    static_shadows: BoolProperty(
        name="Static Shadows",
        description="unk15",
        update=lambda self, context: self.update_optional_flags(context),
    )

    dynamic_shadows: BoolProperty(
        name="Dynamic Shadows",
        update=lambda self, context: self.update_optional_flags(context),
    )

    use_sun_color: BoolProperty(
        name="Use Sun Color",
        description="unk16",
        update=lambda self, context: self.update_optional_flags(context),
    )

    buzzing: BoolProperty(
        name="Enable Buzzing",
        description="unk19",
        update=lambda self, context: self.update_optional_flags(context),
    )

    volumetric_lights: BoolProperty(
        name="Cast Volume",
        update=lambda self, context: self.update_optional_flags(context),
    )

    no_corona_reflection: BoolProperty(
        name="No Corona Reflection",
        update=lambda self, context: self.update_optional_flags(context),
    )

    # Flashiness Flags
    flashiness_flags: EnumProperty(
        name="Flashiness",
        items=[
            ("off", "Off", ""),
            ("rand", "Random", ""),
            ("hazard", "Hazard", ""),
            ("v_slow_hazard", "Very Slow Hazard", ""),
            ("slow_hazard", "Slow Hazard", ""),
            ("fast_hazard", "Fast Hazard", ""),
            ("v_fast_hazard", "Very Fast Hazard", ""),
        ],
        default="off",
        update=lambda self, context: self.update_optional_flags(context),
    )

    # Fade In/Out Flags
    fade_flags: EnumProperty(
        name="Fade in/out",
        items=[("off", "Off", ""), ("v_slow", "Very Slow", ""), ("slow", "Slow", "")],
        default="off",
        update=lambda self, context: self.update_optional_flags(context),
    )

    # Flickering Flags
    misc_flags: EnumProperty(
        name="Misc Flags",
        items=[
            ("None", "None", ""),
            ("f0", "Flickering I", "3s flicker, 5s cooldown"),
            ("f1", "Flickering II", "same as Flickering I, but shifted by 5s"),
            ("f0 f3", "Flashing", ""),
            ("f0 f2", "Slow blinking", ""),
            ("f2", "Medium blinking", ""),
            ("f1 f2 f3", "Fast blinking I", "5s blinking, 10s cooldown"),
            ("f0 f1", "Fast blinking II", "same as Fast blinking I, but shifted by 5s"),
            ("f0 f1 f3", "Fade I", "5sec on, 10sec off state\ntime includes time spent in fade in/out state"),
            ("f2 f3", "Fade II", "same as Fade I, but shifted by 5s"),
            ("f0 f2 f3", "Fade III", "same as Fade I, but shifted by 10s"),
            (
                "f1 f3",
                "Only when raining",
                "Light only turns on when it's raining\nLight intensity depends on rain intensity",
            ),
            ("f3", "Off", "f3"),
            ("f0 f1 f2", "Unknown", "f0 f1 f2, Off?"),
            ("f1 f2", "Unknown", "No effect?"),
        ],
        default="None",
    )

    # unknown flags
    unk18: BoolProperty(name="unk18", update=lambda self, context: self.update_optional_flags(context))
    unk22: BoolProperty(name="unk22", update=lambda self, context: self.update_optional_flags(context))
    unk23: BoolProperty(name="unk23", update=lambda self, context: self.update_optional_flags(context))
    unk24: BoolProperty(name="unk24", update=lambda self, context: self.update_optional_flags(context))

    def update_flag(self, context):
        if self.updating:
            return
        self.updating = True

        time_flag_map = {32: "day", 64: "night", 96: "always_on"}

        flashiness_flag_map = {
            2: "rand",
            4: "hazard",
            8: "v_slow_hazard",
            16: "slow_hazard",
            256: "fast_hazard",
            512: "v_fast_hazard",
        }

        fade_flag_map = {
            1024: "v_slow",
            4096: "slow",
        }

        flag_int = int(self.flag)

        self.time_flag = time_flag_map.get(flag_int & 96, "night")
        self.flashiness_flags = flashiness_flag_map.get(flag_int & 798, "off")
        self.fade_flags = fade_flag_map.get(flag_int & 5120, "off")

        self.weak_light = bool(flag_int & 128)
        self.static_shadows = bool(flag_int & 16384)
        self.dynamic_shadows = bool(flag_int & 32768)
        self.use_sun_color = bool(flag_int & 65536)
        self.unk18 = bool(flag_int & 131072)
        self.buzzing = bool(flag_int & 262144)
        self.volumetric_lights = bool(flag_int & 524288)
        self.no_corona_reflection = bool(flag_int & 1048576)
        self.unk22 = bool(flag_int & 2097152)
        self.unk23 = bool(flag_int & 4194304)
        self.unk24 = bool(flag_int & 8388608)

        update_show_cone(context, self.volumetric_lights)
        self.updating = False

    def update_optional_flags(self, context):
        if self.updating:
            return
        self.updating = True

        flag_int = 0

        match self.time_flag:
            case "day":
                flag_int += 32
            case "night":
                flag_int += 64
            case "all_time":
                flag_int += 96

        match self.flashiness_flags:
            case "rand":
                flag_int += 2
            case "hazard":
                flag_int += 4
            case "slow_hazard":
                flag_int += 8
            case "v_slow_hazard":
                flag_int += 16
            case "v_fast_hazard":
                flag_int += 256
            case "fast_hazard":
                flag_int += 512

        match self.fade_flags:
            case "v_slow":
                flag_int += 1024
            case "slow":
                flag_int += 4096

        flag_int |= 128 if self.weak_light else 0
        flag_int |= 16384 if self.static_shadows else 0
        flag_int |= 32768 if self.dynamic_shadows else 0
        flag_int |= 65536 if self.use_sun_color else 0
        flag_int |= 262144 if self.buzzing else 0
        flag_int |= 524288 if self.volumetric_lights else 0
        flag_int |= 1048576 if self.no_corona_reflection else 0

        flag_int |= 131072 if self.unk18 else 0
        flag_int |= 2097152 if self.unk22 else 0
        flag_int |= 4194304 if self.unk23 else 0
        flag_int |= 8388608 if self.unk24 else 0

        update_show_cone(context, self.volumetric_lights)
        self.flag = str(flag_int)
        self.updating = False


def search_shader(self, context, edit_text):
    return shaders


class Shader(PropertyGroup):
    type: StringProperty(
        name="Type",
        default="gta_default",
        search=search_shader,
    )


def register():
    class_list = [Mtl, GTAIVBone, LightFlags, LightAttributes, Shader]

    for x in class_list:
        try_unregister_class(x)
        register_class(x)

    Mesh.mtl = PointerProperty(type=Mtl)
    Bone.attr = PointerProperty(type=GTAIVBone)
    # Register on the unified Light type (covers both point and spot in Blender 4.x).
    # If the build still has separate types, register on both but guard against duplicates.
    _PointLight.flags = PointerProperty(type=LightFlags)
    _PointLight.attributes = PointerProperty(type=LightAttributes)
    if _SpotLight is not _PointLight:
        _SpotLight.flags = PointerProperty(type=LightFlags)
        _SpotLight.attributes = PointerProperty(type=LightAttributes)
    Material.shader = PointerProperty(type=Shader)


def unregister():
    unregister_class(Mtl)
    unregister_class(GTAIVBone)
    unregister_class(LightAttributes)
    unregister_class(LightFlags)
    unregister_class(Shader)
    del Mesh.mtl
    del Bone.attr
    del _PointLight.flags
    del _PointLight.attributes
    if _SpotLight is not _PointLight:
        try:
            del _SpotLight.flags
            del _SpotLight.attributes
        except Exception:
            pass
    del Material.shader
