"""Native free-3D sketch support built on Blender Curves.

The 2D sketch path remains unchanged. 3D sketches are marked on the Curves
object and use the same curve_id based geometry/constraint storage, while the
solver creates free-in-3D SolveSpace entities for points and lines.
"""

import logging

import bpy
from mathutils import Matrix, Vector
from mathutils.geometry import intersect_point_line

from .. import curve_solver as _curve_solver
from .constants import BezierHandleType, SketchCurveType
from .curve_ref import (
    LineRef,
    PointRef,
    _allocate,
    _ensure_attrs,
    _ensure_curve_data,
    _invalidate,
)
from .distance import SlvsDistance
from .sketch_ref import Sketch, stamp_sketch_props

logger = logging.getLogger(__name__)

SKETCH_3D_TAG = "is_3d_sketch"


def is_3d_sketch(sketch):
    """Return whether *sketch* is a native free-3D sketch."""
    obj = getattr(sketch, "target_object", sketch)
    return bool(obj and obj.get(SKETCH_3D_TAG, False))


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


def _rebuild_3d_lines(sketch):
    """Sync native line curve points from their referenced 3D point curves."""
    from ..utilities.curve_data import read_uuid_list

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

    from ..utilities.curve_data import compute_merge_ids

    compute_merge_ids(sketch)
    curve_data.update_tag()


class NativeCurveSolver(_curve_solver.CurveSolver):
    """CurveSolver extension that dispatches native 3D sketches to free 3D DOF."""

    def _init_workplane(self):
        if not is_3d_sketch(self.sketch):
            return super()._init_workplane()
        self._wp_handle = self.solvesys.E_FREE_IN_3D
        self._normal_handle = None

    def _init_geometry(self):
        if not is_3d_sketch(self.sketch):
            return super()._init_geometry()

        sketch = self.sketch
        if not sketch.target_object or not sketch.target_object.data:
            return

        curve_data = sketch.target_object.data
        type_attr = curve_data.attributes.get("sketch_type")
        if not type_attr:
            return

        from ..utilities.curve_data import has_uuid_field, read_uuid_list

        if not has_uuid_field(curve_data, "curve_id"):
            return

        cid_list = read_uuid_list(curve_data, "curve_id")
        sp_list = read_uuid_list(curve_data, "start_point_id")
        ep_list = read_uuid_list(curve_data, "end_point_id")
        fixed_attr = curve_data.attributes.get("fixed")

        for curve_idx, cid in enumerate(cid_list):
            if type_attr.data[curve_idx].value != SketchCurveType.POINT:
                continue
            curve_slice = curve_data.curves[curve_idx]
            if not curve_slice.points_length:
                continue
            point_index = curve_slice.points[0].index
            position = curve_data.points[point_index].position
            fixed = bool(fixed_attr.data[curve_idx].value) if fixed_attr else False
            group = self.group_fixed if fixed else self.group_sketch
            handle = self.solvesys.add_point_3d(group, *map(float, position[:3]))
            self._point_handles[cid] = handle
            self._entity_handles[cid] = handle

        for curve_idx, cid in enumerate(cid_list):
            if type_attr.data[curve_idx].value != SketchCurveType.LINE:
                continue
            p1_handle = self._point_handles.get(sp_list[curve_idx])
            p2_handle = self._point_handles.get(ep_list[curve_idx])
            if p1_handle and p2_handle:
                self._entity_handles[cid] = self.solvesys.add_line_3d(
                    self.group_sketch, p1_handle, p2_handle
                )

    def _init_constraints(self):
        if not is_3d_sketch(self.sketch):
            return super()._init_constraints()

        sketch_obj = self.sketch.target_object
        if not sketch_obj or not sketch_obj.data:
            return

        self._constraint_by_handle = {}
        for constraint in sketch_obj.data.sketch_constraints.all:
            constraint.failed = False
            if getattr(constraint, "type", "") != "DISTANCE":
                continue
            if not getattr(constraint, "curve_id_1", ""):
                continue
            try:
                handles = constraint.create_slvs_data_from_curves(
                    self.solvesys,
                    self._entity_handles,
                    self.solvesys.E_FREE_IN_3D,
                    self.group_sketch,
                )
            except Exception as exc:
                logger.debug("3D distance constraint init failed: %s", exc)
                continue
            if handles is None:
                continue
            for item in handles if isinstance(handles, (list, tuple)) else (handles,):
                handle = item.get("h") if isinstance(item, dict) else item
                if handle:
                    self._constraint_by_handle[handle] = constraint

    def _get_solved_point_position(self, curve_id):
        if not is_3d_sketch(self.sketch):
            return super()._get_solved_point_position(curve_id)
        handle = self._point_handles.get(curve_id)
        if not handle:
            return None
        return tuple(
            self.solvesys.get_param_value(handle["param"][axis]) for axis in range(3)
        )

    def _write_results(self):
        if not is_3d_sketch(self.sketch):
            return super()._write_results()

        from ..utilities.curve_data import read_uuid_list

        curve_data = self.sketch.target_object.data
        type_attr = curve_data.attributes.get("sketch_type")
        if not type_attr:
            return
        cid_list = read_uuid_list(curve_data, "curve_id")

        for curve_idx, cid in enumerate(cid_list):
            if type_attr.data[curve_idx].value != SketchCurveType.POINT:
                continue
            position = self._get_solved_point_position(cid)
            if position is None:
                continue
            point_index = curve_data.curves[curve_idx].points[0].index
            curve_data.points[point_index].position = position

        _rebuild_3d_lines(self.sketch)


_ORIGINAL_CURVE_SOLVER = _curve_solver.CurveSolver
_ORIGINAL_SOLVER_ALIAS = _curve_solver.Solver
_ORIGINAL_DISTANCE_MATRIX_BASIS = SlvsDistance.matrix_basis
_ORIGINAL_DISTANCE_INIT_VALUE = SlvsDistance._get_init_value
_ORIGINAL_DISTANCE_USE_ALIGN = SlvsDistance.use_align
_ORIGINAL_DISTANCE_USE_FLIPPING = SlvsDistance.use_flipping


def _distance_is_3d(constraint):
    sketch = constraint._get_sketch()
    return bool(sketch and is_3d_sketch(sketch))


def _distance_use_align(self):
    if _distance_is_3d(self):
        return False
    return _ORIGINAL_DISTANCE_USE_ALIGN(self)


def _distance_use_flipping(self):
    if _distance_is_3d(self):
        return False
    return _ORIGINAL_DISTANCE_USE_FLIPPING(self)


def _distance_init_value(self, alignment):
    if not _distance_is_3d(self):
        return _ORIGINAL_DISTANCE_INIT_VALUE(self, alignment)

    r1, r2 = self.ref(1), self.ref(2)
    if not r1:
        return self.value_store if self.is_property_set("value_store") else 0.0
    if r1.is_line():
        p1, p2 = r1.p1, r1.p2
        return (p2.location - p1.location).length if p1 and p2 else 0.0
    if r1.is_point() and r2 and r2.is_point():
        return (r2.location - r1.location).length
    if r1.is_point() and r2 and r2.is_line():
        nearest, _ = intersect_point_line(r1.location, r2.p1.location, r2.p2.location)
        return (nearest - r1.location).length
    return 0.0


def _distance_matrix_basis(self):
    if not _distance_is_3d(self):
        return _ORIGINAL_DISTANCE_MATRIX_BASIS(self)

    r1, r2 = self.ref(1), self.ref(2)
    if not r1:
        return Matrix.Identity(4)

    if r1.is_line() and not r2:
        p1 = r1.p1.location
        p2 = r1.p2.location
    elif r1.is_point() and r2 and r2.is_point():
        p1 = r1.location
        p2 = r2.location
    elif r1.is_point() and r2 and r2.is_line():
        p1 = r1.location
        p2, _ = intersect_point_line(p1, r2.p1.location, r2.p2.location)
    else:
        return Matrix.Identity(4)

    direction = p2 - p1
    midpoint = (p1 + p2) / 2.0
    if direction.length <= 1e-12:
        return Matrix.Translation(midpoint)

    rotation = direction.normalized().to_track_quat("X", "Z").to_matrix().to_4x4()
    return Matrix.Translation(midpoint) @ rotation


def register():
    _curve_solver.CurveSolver = NativeCurveSolver
    _curve_solver.Solver = NativeCurveSolver
    SlvsDistance.matrix_basis = _distance_matrix_basis
    SlvsDistance._get_init_value = _distance_init_value
    SlvsDistance.use_align = _distance_use_align
    SlvsDistance.use_flipping = _distance_use_flipping


def unregister():
    SlvsDistance.use_flipping = _ORIGINAL_DISTANCE_USE_FLIPPING
    SlvsDistance.use_align = _ORIGINAL_DISTANCE_USE_ALIGN
    SlvsDistance._get_init_value = _ORIGINAL_DISTANCE_INIT_VALUE
    SlvsDistance.matrix_basis = _ORIGINAL_DISTANCE_MATRIX_BASIS
    _curve_solver.Solver = _ORIGINAL_SOLVER_ALIAS
    _curve_solver.CurveSolver = _ORIGINAL_CURVE_SOLVER
