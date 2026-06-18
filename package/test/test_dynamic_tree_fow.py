import sys
import unittest
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS))

from compute_ward_occlusion_native import normalize_tree_events
from native_fow import VisibilityGrid


class DynamicTreeFowTests(unittest.TestCase):
    def make_grid(self):
        return VisibilityGrid(
            width=3,
            height=2,
            origin_x=0.0,
            origin_y=0.0,
            cell_size=64.0,
            tile_bytes=[0, 0x81, 0, 0, 0, 0],
            hard_visible=None,
            metadata={},
        )

    def test_tree_bit_can_be_cleared_and_restored(self):
        grid = self.make_grid()
        self.assertTrue(grid.tree_alive_at(1, 0))
        self.assertTrue(grid.set_tree_alive(1, 0, False))
        self.assertEqual(grid.tile_byte_at(1, 0), 0x01)
        self.assertTrue(grid.set_tree_alive(1, 0, True))
        self.assertEqual(grid.tile_byte_at(1, 0), 0x81)

    def test_events_are_normalized_to_effective_integer_seconds(self):
        grid = self.make_grid()
        events, rejected = normalize_tree_events(
            [
                {"time": 10.2, "event_type": "death", "grid_x": 1, "grid_y": 0},
                {"time": 20, "event_type": "respawn", "world_x": 80, "world_y": 16},
            ],
            grid,
            {},
        )
        self.assertEqual(rejected, [])
        self.assertEqual([event["second"] for event in events], [11, 20])
        self.assertEqual([event["alive"] for event in events], [False, True])
        self.assertEqual(events[0]["cell"], [1, 0])

    def test_non_tree_cells_are_rejected(self):
        grid = self.make_grid()
        events, rejected = normalize_tree_events(
            [{"time": 1, "event_type": "death", "grid_x": 0, "grid_y": 0}],
            grid,
            {},
        )
        self.assertEqual(events, [])
        self.assertIn("not an initial static tree cell", rejected[0]["reason"])


if __name__ == "__main__":
    unittest.main()
