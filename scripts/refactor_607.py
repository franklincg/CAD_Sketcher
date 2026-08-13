from pathlib import Path


def replace_once(path, old, new):
    p = Path(path)
    text = p.read_text()
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"Expected one match in {path}, got {count}: {old[:80]!r}")
    p.write_text(text.replace(old, new, 1))


# Sketch model: make the free-3D distinction a first-class sketch property.
replace_once(
    "model/sketch_ref.py",
    '_DOF = "dof"\n',
    '_DOF = "dof"\n_IS_3D = "is_3d_sketch"\n',
)
replace_once(
    "model/sketch_ref.py",
    "    @property\n    def workplane_object(self):\n        return self._obj.parent\n\n",
    "    @property\n    def workplane_object(self):\n        return self._obj.parent\n\n"
    "    @property\n    def is_3d(self):\n"
    '        """Whether this sketch is free in 3D instead of workplane-bound."""\n'
    "        return bool(self._obj.get(_IS_3D, False))\n\n",
)

# native_3d is now a helper module, not a registration-time monkey patch.
replace_once("model/__init__.py", '    "native_3d",\n', "")

Path("model/native_3d.py").write_text(
    '''"""Helpers for native free-3D sketches backed by Blender Curves."""

import bpy
from mathutils import Vector

from .constants import BezierHandleType, SketchCurveType
from .curve_ref import (
    LineRef,
    PointRef,
    _allocate,
    _ensure_attrs,
    _ensure_curve_data,
    _invalidate,
)
from .sketch_ref import Sketch, stamp_sketch_props

SKETCH_3D_TAG = "is_3d_sketch"


def is_3d_sketch(sketch):
    """Return whether *sketch* is a native free-3D sketch."""
    return bool(sketch and getattr(sketch, "is_3d", False))


def create_3d_sketch(context, name="3D Sketch"):
    """Create an unparented Curves-backed 3D sketch and return its accessor."""
    curve = bpy.data.hair_curves.new(name)
    obj = bpy.data.objects.new(name, curve)
    context.scene.collection.objects.link(obj)
    stamp_sketch_props(obj)
    obj[SKETCH_3D_TAG] = True

    from ..utilities.curve_data import _ensure_convert_modifier

    _ensure_convert_modifier(obj)
    return Sketch(obj)


def _local_point_position(ref):
    if not isinstance(ref, PointRef) or not ref._resolve():
        return Vector((0.0, 0.0, 0.0))
    point_index = ref._curve_slice.points[0].index
    return Vector(ref._curve_data.points[point_index].position)


def create_point_3d(sketch, co, construction=False, fixed=False, name=None):
    """Create a native point curve with an XYZ position in a 3D sketch."""
    if not is_3d_sketch(sketch):
        raise ValueError("create_point_3d requires a native 3D sketch")

    from ..utilities.curve_data import default_curve_name, set_attribute

    curve_data = _ensure_curve_data(sketch)
    if curve_data is None:
        return None

    position = Vector(co).to_3d()
    cid = _allocate(sketch)
    curve_data.add_curves([1])
    _ensure_attrs(curve_data, len(curve_data.curves) - 1)

    curve_idx = len(curve_data.curves) - 1
    curve_slice = curve_data.curves[curve_idx]
    curve_slice.points[0].position = tuple(position)

    attrs = curve_data.attributes
    set_attribute(attrs, "curve_id", cid, curve_idx)
    set_attribute(attrs, "sketch_type", SketchCurveType.POINT, curve_idx)
    set_attribute(attrs, "construction", construction, curve_idx)
    set_attribute(attrs, "fixed", fixed, curve_idx)
    set_attribute(attrs, "visible", True, curve_idx)
    set_attribute(
        attrs,
        "name",
        name or default_curve_name(curve_data, SketchCurveType.POINT),
        curve_idx,
    )

    _invalidate(sketch)
    curve_data.update_tag()
    return PointRef(sketch, cid)


def create_line_3d(sketch, p1, p2, construction=False, name=None):
    """Create a native line curve between two 3D point curves."""
    if not is_3d_sketch(sketch):
        raise ValueError("create_line_3d requires a native 3D sketch")
    if not isinstance(p1, PointRef) or not isinstance(p2, PointRef):
        raise TypeError("3D lines require PointRef endpoints")

    from ..utilities.curve_data import default_curve_name, set_attribute

    curve_data = _ensure_curve_data(sketch)
    if curve_data is None:
        return None

    cid = _allocate(sketch)
    curve_data.add_curves([2])
    curve_data.set_types(type="BEZIER")
    _ensure_attrs(curve_data, len(curve_data.curves) - 1)

    curve_idx = len(curve_data.curves) - 1
    curve_slice = curve_data.curves[curve_idx]
    positions = (_local_point_position(p1), _local_point_position(p2))

    attrs = curve_data.attributes
    for point, position in zip(curve_slice.points, positions):
        curve_data.points[point.index].position = tuple(position)
        attrs["handle_left"].data[point.index].vector = position
        attrs["handle_right"].data[point.index].vector = position
        attrs["handle_type_left"].data[point.index].value = BezierHandleType.FREE
        attrs["handle_type_right"].data[point.index].value = BezierHandleType.FREE

    set_attribute(attrs, "curve_id", cid, curve_idx)
    set_attribute(attrs, "sketch_type", SketchCurveType.LINE, curve_idx)
    set_attribute(attrs, "start_point_id", p1.curve_id, curve_idx)
    set_attribute(attrs, "end_point_id", p2.curve_id, curve_idx)
    set_attribute(attrs, "construction", construction, curve_idx)
    set_attribute(attrs, "fixed", False, curve_idx)
    set_attribute(attrs, "visible", True, curve_idx)
    set_attribute(
        attrs,
        "name",
        name or default_curve_name(curve_data, SketchCurveType.LINE),
        curve_idx,
    )

    _invalidate(sketch)
    curve_data.update_tag()
    return LineRef(sketch, cid)


def rebuild_3d_lines(sketch):
    """Sync native line geometry from its referenced 3D point curves."""
    from ..utilities.curve_data import compute_merge_ids, read_uuid_list

    curve_data = sketch.target_object.data
    type_attr = curve_data.attributes.get("sketch_type")
    if not type_attr:
        return

    cid_list = read_uuid_list(curve_data, "curve_id")
    sp_list = read_uuid_list(curve_data, "start_point_id")
    ep_list = read_uuid_list(curve_data, "end_point_id")

    point_positions = {}
    for curve_idx, cid in enumerate(cid_list):
        if type_attr.data[curve_idx].value != SketchCurveType.POINT:
            continue
        curve_slice = curve_data.curves[curve_idx]
        if not curve_slice.points_length:
            continue
        point_index = curve_slice.points[0].index
        point_positions[cid] = Vector(curve_data.points[point_index].position)

    handle_left = curve_data.attributes.get("handle_left")
    handle_right = curve_data.attributes.get("handle_right")

    for curve_idx in range(len(curve_data.curves)):
        if type_attr.data[curve_idx].value != SketchCurveType.LINE:
            continue
        curve_slice = curve_data.curves[curve_idx]
        if curve_slice.points_length < 2:
            continue

        for point_offset, cid in enumerate((sp_list[curve_idx], ep_list[curve_idx])):
            position = point_positions.get(cid)
            if position is None:
                continue
            point_index = curve_slice.points[point_offset].index
            curve_data.points[point_index].position = tuple(position)
            if handle_left:
                handle_left.data[point_index].vector = position
            if handle_right:
                handle_right.data[point_index].vector = position

    compute_merge_ids(sketch)
    curve_data.update_tag()
'''
)

# CurveSolver: directly dispatch free-3D sketches to SolveSpace 3D entities.
replace_once(
    "curve_solver.py",
    '        Falls back to entity workplane if no empty exists yet.\n        """\n        wp_obj = ensure_workplane_empty(self.sketch)\n',
    '        Falls back to entity workplane if no empty exists yet.\n        """\n'
    '        if getattr(self.sketch, "is_3d", False):\n'
    '            self._wp_handle = self.solvesys.E_FREE_IN_3D\n'
    '            self._normal_handle = None\n'
    '            return\n\n'
    '        wp_obj = ensure_workplane_empty(self.sketch)\n',
)
replace_once(
    "curve_solver.py",
    "        wp = self._wp_handle\n\n        from .utilities.curve_data import read_uuid_list\n",
    "        wp = self._wp_handle\n"
    '        is_3d = getattr(sketch, "is_3d", False)\n\n'
    "        from .utilities.curve_data import read_uuid_list\n",
)
replace_once(
    "curve_solver.py",
    "            pt_idx = curve_data.curves[curve_idx].points[0].index\n"
    "            pos = curve_data.points[pt_idx].position\n"
    "            u, v = float(pos[0]), float(pos[1])\n\n"
    "            handle = self.solvesys.add_point_2d(group, u, v, wp)\n",
    "            pt_idx = curve_data.curves[curve_idx].points[0].index\n"
    "            pos = curve_data.points[pt_idx].position\n\n"
    "            if is_3d:\n"
    "                handle = self.solvesys.add_point_3d(\n"
    "                    group, *map(float, pos[:3])\n"
    "                )\n"
    "            else:\n"
    "                u, v = float(pos[0]), float(pos[1])\n"
    "                handle = self.solvesys.add_point_2d(group, u, v, wp)\n",
)
replace_once(
    "curve_solver.py",
    "            ctype = type_attr.data[curve_idx].value\n"
    "            cid = cid_list[curve_idx]\n\n"
    "            if ctype == SketchCurveType.LINE:\n",
    "            ctype = type_attr.data[curve_idx].value\n"
    "            cid = cid_list[curve_idx]\n\n"
    "            if is_3d and ctype not in (SketchCurveType.POINT, SketchCurveType.LINE):\n"
    "                continue\n\n"
    "            if ctype == SketchCurveType.LINE:\n",
)
replace_once(
    "curve_solver.py",
    "                if p1_handle and p2_handle:\n"
    "                    handle = self.solvesys.add_line_2d(\n"
    "                        self.group_sketch, p1_handle, p2_handle, wp\n"
    "                    )\n"
    "                    self._entity_handles[cid] = handle\n",
    "                if p1_handle and p2_handle:\n"
    "                    if is_3d:\n"
    "                        handle = self.solvesys.add_line_3d(\n"
    "                            self.group_sketch, p1_handle, p2_handle\n"
    "                        )\n"
    "                    else:\n"
    "                        handle = self.solvesys.add_line_2d(\n"
    "                            self.group_sketch, p1_handle, p2_handle, wp\n"
    "                        )\n"
    "                    self._entity_handles[cid] = handle\n",
)
replace_once(
    "curve_solver.py",
    "        if self._tweak_curve_id is not None and self._tweak_pos is not None:\n",
    "        if (\n"
    "            not is_3d\n"
    "            and self._tweak_curve_id is not None\n"
    "            and self._tweak_pos is not None\n"
    "        ):\n",
)
replace_once(
    "curve_solver.py",
    "            group = self.group_sketch\n            c.failed = False\n\n"
    "            if not getattr(c, \"curve_id_1\", \"\"):\n",
    "            group = self.group_sketch\n            c.failed = False\n\n"
    "            if getattr(sketch, \"is_3d\", False) and getattr(c, \"type\", \"\") != \"DISTANCE\":\n"
    "                continue\n\n"
    "            if not getattr(c, \"curve_id_1\", \"\"):\n",
)
replace_once(
    "curve_solver.py",
    "        u = self.solvesys.get_param_value(handle[\"param\"][0])\n"
    "        v = self.solvesys.get_param_value(handle[\"param\"][1])\n"
    "        return (u, v, 0.0)\n",
    "        if getattr(self.sketch, \"is_3d\", False):\n"
    "            return tuple(\n"
    "                self.solvesys.get_param_value(handle[\"param\"][axis])\n"
    "                for axis in range(3)\n"
    "            )\n"
    "        u = self.solvesys.get_param_value(handle[\"param\"][0])\n"
    "        v = self.solvesys.get_param_value(handle[\"param\"][1])\n"
    "        return (u, v, 0.0)\n",
)
replace_once(
    "curve_solver.py",
    "        # Third pass: rebuild segments from updated point positions\n"
    "        from .utilities.curve_data import rebuild_segments\n\n"
    "        rebuild_segments(sketch)\n",
    "        # Third pass: rebuild segments from updated point positions.\n"
    "        if getattr(sketch, \"is_3d\", False):\n"
    "            from .model.native_3d import rebuild_3d_lines\n\n"
    "            rebuild_3d_lines(sketch)\n"
    "        else:\n"
    "            from .utilities.curve_data import rebuild_segments\n\n"
    "            rebuild_segments(sketch)\n",
)

# Distance: make native 3D geometry a normal code path, including world-space gizmos.
replace_once(
    "model/distance.py",
    "    @classmethod\n    def get_types(cls, index, entities):\n",
    "    def _is_native_3d(self):\n"
    "        sketch = self._get_sketch()\n"
    "        return bool(sketch and getattr(sketch, \"is_3d\", False))\n\n"
    "    @classmethod\n    def get_types(cls, index, entities):\n",
)
replace_once(
    "model/distance.py",
    "    def use_flipping(self):\n"
    "        # Only use flipping for constraint between point and line/workplane\n"
    "        r1, r2 = self.ref(1), self.ref(2)\n",
    "    def use_flipping(self):\n"
    "        # Only use flipping for constraint between point and line/workplane\n"
    "        if self._is_native_3d():\n"
    "            return False\n"
    "        r1, r2 = self.ref(1), self.ref(2)\n",
)
replace_once(
    "model/distance.py",
    "    def use_align(self):\n"
    '        """Returns True if constraint\'s entities allow distance to be aligned"""\n'
    "        r1, r2 = self.ref(1), self.ref(2)\n",
    "    def use_align(self):\n"
    '        """Returns True if constraint\'s entities allow distance to be aligned"""\n'
    "        if self._is_native_3d():\n"
    "            return False\n"
    "        r1, r2 = self.ref(1), self.ref(2)\n",
)
replace_once(
    "model/distance.py",
    "    def matrix_basis(self):\n"
    "        r1, r2 = self.ref(1), self.ref(2)\n"
    "        if not r1 or not r1.valid:\n"
    "            return Matrix()\n"
    "        return self._compute_matrix_basis(r1, r2, r1.wp_matrix)\n\n",
    "    def matrix_basis(self):\n"
    "        r1, r2 = self.ref(1), self.ref(2)\n"
    "        if not r1 or not r1.valid:\n"
    "            return Matrix()\n"
    "        if self._is_native_3d():\n"
    "            return self._compute_matrix_basis_3d(r1, r2)\n"
    "        return self._compute_matrix_basis(r1, r2, r1.wp_matrix)\n\n"
    "    def _compute_matrix_basis_3d(self, e1, e2):\n"
    '        """Build a world-space dimension frame for native 3D geometry."""\n'
    "        if e1.is_line():\n"
    "            p1, p2 = e1.p1.location, e1.p2.location\n"
    "        elif e1.is_point() and e2 and e2.is_point():\n"
    "            p1, p2 = e1.location, e2.location\n"
    "        elif e1.is_point() and e2 and e2.is_line():\n"
    "            p1 = e1.location\n"
    "            p2, _ = intersect_point_line(p1, e2.p1.location, e2.p2.location)\n"
    "        else:\n"
    "            return Matrix.Identity(4)\n\n"
    "        direction = p2 - p1\n"
    "        midpoint = (p1 + p2) / 2.0\n"
    "        if direction.length <= 1e-12:\n"
    "            return Matrix.Translation(midpoint)\n\n"
    "        rotation = (\n"
    "            direction.normalized().to_track_quat(\"X\", \"Z\").to_matrix().to_4x4()\n"
    "        )\n"
    "        return Matrix.Translation(midpoint) @ rotation\n\n",
)
replace_once(
    "model/distance.py",
    "        if not r1:\n"
    "            if self.is_property_set(\"value_store\"):\n"
    "                return self.value_store\n"
    "            return 0.0\n\n"
    "        if r1.is_line():\n",
    "        if not r1:\n"
    "            if self.is_property_set(\"value_store\"):\n"
    "                return self.value_store\n"
    "            return 0.0\n\n"
    "        if self._is_native_3d():\n"
    "            if r1.is_line():\n"
    "                p1, p2 = r1.p1, r1.p2\n"
    "                return (p2.location - p1.location).length if p1 and p2 else 0.0\n"
    "            if r1.is_point() and r2 and r2.is_point():\n"
    "                return (r2.location - r1.location).length\n"
    "            if r1.is_point() and r2 and r2.is_line():\n"
    "                nearest, _ = intersect_point_line(\n"
    "                    r1.location, r2.p1.location, r2.p2.location\n"
    "                )\n"
    "                return (nearest - r1.location).length\n"
    "            return 0.0\n\n"
    "        if r1.is_line():\n",
)
