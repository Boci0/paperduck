"""Pure geometry for perching on window top edges (src/ui/perch.py)."""

from __future__ import annotations

from src.ui.perch import perch_covered, perch_lost, perch_position, pick_perch, to_logical

MON = (0, 0, 1920, 1080)
TASKBAR = (0, 1032, 1920, 1080)
WIN = (400, 300, 1200, 900)


class TestPickPerch:
    def test_feet_near_top_edge_pick_it(self) -> None:
        assert pick_perch([WIN, TASKBAR], 600, 270) == 0
        assert pick_perch([WIN, TASKBAR], 600, 335) == 0  # dropped a little into the window

    def test_out_of_reach_or_beside_the_edge(self) -> None:
        assert pick_perch([WIN], 600, 259) is None
        assert pick_perch([WIN], 600, 341) is None
        assert pick_perch([WIN], 1200, 300) is None  # right edge is exclusive
        assert pick_perch([WIN], 399, 300) is None

    def test_taskbar(self) -> None:
        assert pick_perch([WIN, TASKBAR], 1500, 1020) == 1

    def test_covered_edge_is_skipped_for_the_next_one(self) -> None:
        above = (500, 200, 800, 700)  # covers WIN's top edge at x=600
        assert pick_perch([above, WIN], 600, 300) is None  # above's own edge is 100px away
        lower = (300, 310, 1300, 800)
        assert pick_perch([above, WIN, lower], 1000, 305) == 1  # first in z-order wins where both are in reach
        assert pick_perch([above, WIN, lower], 600, 305) is None
        assert pick_perch([above, lower], 1000, 305) == 1


class TestPerchPosition:
    def test_feet_on_edge_centre_kept(self) -> None:
        # sprite centre 88 and feet 141 in window coordinates (pet scale 2)
        assert perch_position(WIN, 600, 88, 141, 16) == (512, 159)

    def test_centre_clamped_inside_the_ends(self) -> None:
        assert perch_position(WIN, 100, 88, 141, 16) == (416 - 88, 159)
        assert perch_position(WIN, 5000, 88, 141, 16) == (1184 - 88, 159)

    def test_follow_move_and_resize(self) -> None:
        offset = 600 - WIN[0]
        moved = (WIN[0] + 50, WIN[1] - 20, WIN[2] + 50, WIN[3] - 20)
        assert perch_position(moved, moved[0] + offset, 88, 141, 16) == (650 - 88, 280 - 141)
        narrowed = (400, 300, 500, 900)  # resized narrower than the offset: slides back onto the edge
        assert perch_position(narrowed, narrowed[0] + offset, 88, 141, 16) == (484 - 88, 159)

    def test_edge_narrower_than_margins_centres(self) -> None:
        assert perch_position((400, 300, 420, 900), 600, 88, 141, 16) == (410 - 88, 159)


class TestPerchLost:
    def test_normal_window_kept(self) -> None:
        assert not perch_lost(WIN, [MON], 600)
        assert not perch_lost(TASKBAR, [MON], 600)

    def test_gone_fullscreen_or_off_screen(self) -> None:
        assert perch_lost(None, [MON], 600)
        assert perch_lost(MON, [MON], 600)
        assert perch_lost((-8, -8, 1928, 1088), [MON], 600)  # borderless fullscreen overhang
        assert perch_lost((400, 1200, 1200, 1800), [MON], 600)  # moved below the screen
        assert perch_lost((-900, 300, -100, 900), [MON], -500)

    def test_edge_on_second_monitor(self) -> None:
        second = (1920, 0, 3840, 1080)
        assert not perch_lost((2000, 300, 2600, 900), [MON, second], 2100)


class TestToLogical:
    def test_identity_at_100_percent(self) -> None:
        assert to_logical(WIN, [(MON, 1.0)]) == WIN

    def test_scaled_screen_keeps_its_origin(self) -> None:
        # a 150% monitor right of the primary: Qt keeps its top-left at 1920,0 and scales from there
        screens = [(MON, 1.0), ((1920, 0, 1920 + 1707, 960), 1.5)]
        assert to_logical((1920 + 300, 600, 1920 + 900, 1200), screens) == (2120, 400, 2520, 800)
        assert to_logical((300, 600, 900, 1000), screens) == (300, 600, 900, 1000)


class TestPerchCovered:
    def test_uncovered_and_gone(self) -> None:
        assert not perch_covered([WIN, TASKBAR], 0, 600)
        assert perch_covered([WIN], None, 600)  # cloaked, minimized or closed: not in the list

    def test_window_brought_to_front_over_the_feet(self) -> None:
        front = (500, 100, 900, 700)
        assert perch_covered([front, WIN], 1, 600)
        assert not perch_covered([front, WIN], 1, 1000)  # covers the edge, but not under the feet

    def test_window_below_never_covers(self) -> None:
        assert not perch_covered([WIN, (500, 100, 900, 700)], 0, 600)

    def test_touching_edges_do_not_cover(self) -> None:
        assert not perch_covered([(400, 100, 1200, 300), WIN], 1, 600)  # bottom exclusive, ends at the edge

    def test_taskbar_stays_valid_under_normal_windows(self) -> None:
        # the taskbar is topmost, so ordinary windows sit after it in z-order
        assert not perch_covered([TASKBAR, WIN, MON], 0, 600)

    def test_hop_onto_the_covering_windows_edge(self) -> None:
        front = (450, 290, 900, 800)  # covers the feet, its own top edge 10px above them
        rects = [front, WIN]
        assert perch_covered(rects, 1, 600)
        assert pick_perch(rects, 600, 300) == 0

    def test_right_reserve_keeps_duck_off_caption_buttons_when_wide_enough(self) -> None:
        assert perch_position((0, 200, 800, 900), 790, 88, 141, 16, right_reserve=140) == (644 - 88, 59)
        # narrow window ignores right_reserve so the duck still fits
        assert perch_position((0, 200, 160, 900), 150, 88, 141, 16, right_reserve=140) == (144 - 88, 59)
