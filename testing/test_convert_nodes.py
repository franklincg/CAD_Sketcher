"""The Blender 5.2+ identity-weld convert node group.

Only runs on 5.2+, where the ``Merge Points`` node exists. On older Blender the
convert modifier keeps loading the merge-by-distance asset, so there is nothing
to build here. Besides weld identity, the 5.2 group materializes deterministic
vertex/face ids on the generated mesh for consumers that keep element links.
"""

import unittest
from unittest import TestCase

import bpy

from .utils import Sketch2dTestCase


@unittest.skipIf(bpy.app.version < (5, 2, 0), "Merge Points requires Blender 5.2+")
class TestConvertNodeGroup(TestCase):
    def test_builds_identity_group(self):
        from ..utilities.convert_nodes import (
            _is_identity_group,
            build_convert_node_group,
        )

        ng = build_convert_node_group("test_convert_id")
        try:
            self.assertTrue(_is_identity_group(ng))
            ids = {n.bl_idname for n in ng.nodes}
            for expected in (
                "GeometryNodeMergePoints",
                "GeometryNodeInputMeshVertexNeighbors",
                "GeometryNodeCurveToMesh",
                "GeometryNodeFillCurve",
                "GeometryNodeStoreNamedAttribute",
            ):
                self.assertIn(expected, ids)
        finally:
            bpy.data.node_groups.remove(ng)

    def test_idempotent(self):
        from ..utilities.convert_nodes import build_convert_node_group

        a = build_convert_node_group("test_convert_id2")
        b = build_convert_node_group("test_convert_id2")
        try:
            self.assertIs(a, b)
        finally:
            bpy.data.node_groups.remove(a)

    def test_excludes_zero_id(self):
        """The weld must AND valence with merge_id != 0, so a not-yet-computed
        id (0) can't collapse every endpoint into one point (draw-time glitch)."""
        from ..utilities.convert_nodes import (
            CONVERT_VERSION,
            build_convert_node_group,
        )

        ng = build_convert_node_group("test_convert_zero")
        try:
            self.assertEqual(ng.get("cad_convert_version"), CONVERT_VERSION)
            ands = [
                n
                for n in ng.nodes
                if n.bl_idname == "FunctionNodeBooleanMath" and n.operation == "AND"
            ]
            self.assertTrue(ands, "weld selection does not exclude merge_id 0")
        finally:
            bpy.data.node_groups.remove(ng)

    def test_generated_id_nodes_have_expected_domains(self):
        from ..utilities.convert_nodes import (
            FACE_ID_ATTR,
            VERTEX_ID_ATTR,
            build_convert_node_group,
        )

        ng = build_convert_node_group("test_generated_ids")
        try:
            stores = {
                n.inputs["Name"].default_value: n
                for n in ng.nodes
                if n.bl_idname == "GeometryNodeStoreNamedAttribute"
            }
            self.assertEqual(stores[VERTEX_ID_ATTR].data_type, "INT")
            self.assertEqual(stores[VERTEX_ID_ATTR].domain, "POINT")
            self.assertEqual(stores[FACE_ID_ATTR].data_type, "INT")
            self.assertEqual(stores[FACE_ID_ATTR].domain, "FACE")
        finally:
            bpy.data.node_groups.remove(ng)

    def test_version_marker_rebuilds_stale(self):
        """A group with an old version marker is rebuilt in place (so modifiers
        bound to it upgrade without rebinding)."""
        from ..utilities.convert_nodes import (
            CONVERT_VERSION,
            build_convert_node_group,
        )

        ng = build_convert_node_group("test_convert_stale")
        try:
            ng["cad_convert_version"] = 0
            again = build_convert_node_group("test_convert_stale")
            self.assertIs(again, ng)
            self.assertEqual(ng.get("cad_convert_version"), CONVERT_VERSION)
        finally:
            bpy.data.node_groups.remove(ng)


@unittest.skipIf(bpy.app.version < (5, 2, 0), "Merge Points requires Blender 5.2+")
class TestGeneratedElementIdentity(Sketch2dTestCase):
    def _evaluated_ids(self):
        from ..utilities.convert_nodes import FACE_ID_ATTR, VERTEX_ID_ATTR

        ob = self.sketch.target_object
        ob.update_tag()
        dg = self.context.evaluated_depsgraph_get()
        dg.update()
        evaluated = ob.evaluated_get(dg)
        mesh = evaluated.to_mesh()
        try:
            vertex_attr = mesh.attributes.get(VERTEX_ID_ATTR)
            face_attr = mesh.attributes.get(FACE_ID_ATTR)
            self.assertIsNotNone(vertex_attr)
            self.assertIsNotNone(face_attr)
            vertex_ids = [item.value for item in vertex_attr.data]
            face_ids = [item.value for item in face_attr.data]
            return vertex_ids, face_ids, len(mesh.vertices), len(mesh.polygons)
        finally:
            evaluated.to_mesh_clear()

    def test_ids_survive_coordinate_edit_with_same_topology(self):
        from ..utilities.curve_data import refresh_curve_geometry

        p1 = self.add_point((0.0, 0.0))
        p2 = self.add_point((2.0, 0.0))
        p3 = self.add_point((2.0, 2.0))
        p4 = self.add_point((0.0, 2.0))
        self.add_line(p1, p2)
        self.add_line(p2, p3)
        self.add_line(p3, p4)
        self.add_line(p4, p1)
        refresh_curve_geometry(self.sketch)

        before = self._evaluated_ids()
        self.assertGreater(before[2], 0)
        self.assertGreater(before[3], 0)
        self.assertEqual(before[0], list(range(before[2])))
        self.assertEqual(before[1], list(range(before[3])))

        # Move geometry without changing its connectivity. Generated element
        # identity must remain exactly the same after the GN mesh is rebuilt.
        p2.co = (3.0, 0.5)
        refresh_curve_geometry(self.sketch)
        after = self._evaluated_ids()

        self.assertEqual(after[2:], before[2:])
        self.assertEqual(after[0], before[0])
        self.assertEqual(after[1], before[1])
