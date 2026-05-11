import base64
import json
import os
import unittest

import main


class AutonavGuardrailsTestCase(unittest.TestCase):
    def _load_tick(self, run_id: str, tick_no: int):
        root = os.path.join(os.path.dirname(main.__file__), "autonav_logs", run_id)
        stem = f"tick_{tick_no:04d}"
        with open(os.path.join(root, f"{stem}.json"), "r") as fh:
            tick = json.load(fh)
        with open(os.path.join(root, f"{stem}_front.jpg"), "rb") as fh:
            frame_b64 = base64.b64encode(fh.read()).decode("ascii")
        return tick, frame_b64

    def _load_frame_b64(self, run_id: str, tick_no: int):
        root = os.path.join(os.path.dirname(main.__file__), "autonav_logs", run_id)
        stem = f"tick_{tick_no:04d}"
        with open(os.path.join(root, f"{stem}_front.jpg"), "rb") as fh:
            return base64.b64encode(fh.read()).decode("ascii")

    def test_blocked_center_profile_prefers_right_turn(self):
        tick, frame_b64 = self._load_tick("20260424_093953", 12)
        profile = main._frame_path_profile(
            frame_b64,
            tick["learned_floor_rgb"],
            tick["learned_wall_rgb"],
        )
        self.assertIsNotNone(profile)
        self.assertTrue(profile["center_blocked"])
        self.assertEqual(profile["preferred_turn"], "turn_right")

    def test_clear_center_profile_does_not_mark_blocked(self):
        tick, frame_b64 = self._load_tick("20260424_093953", 7)
        profile = main._frame_path_profile(
            frame_b64,
            tick["learned_floor_rgb"],
            tick["learned_wall_rgb"],
        )
        self.assertIsNotNone(profile)
        self.assertFalse(profile["center_blocked"])

    def test_center_block_override_changes_forward_to_turn(self):
        tick, frame_b64 = self._load_tick("20260424_093953", 12)
        profile = main._frame_path_profile(
            frame_b64,
            tick["learned_floor_rgb"],
            tick["learned_wall_rgb"],
        )
        decision = {
            "action": "forward",
            "linear_speed": 0.25,
            "turn_degrees": 0.0,
            "duration_ms": 700,
            "confidence": 0.9,
            "reason": "The path ahead looks clear.",
            "comment_front": "",
            "comment_rear": "",
            "plan_of_action": "",
            "reasoning_steps": ["a", "b", "c", "d"],
        }

        overridden = main._apply_center_block_override(decision, profile, 90.0)

        self.assertEqual(overridden["action"], "turn_right")
        self.assertEqual(overridden["turn_degrees"], 70.0)
        self.assertIn("center-block-override", overridden["reason"])

    def test_wall_hugging_profile_exposes_open_side_turn(self):
        tick, frame_b64 = self._load_tick("20260424_100519", 49)
        profile = main._frame_path_profile(
            frame_b64,
            tick["learned_floor_rgb"],
            tick["learned_wall_rgb"],
        )
        self.assertIsNotNone(profile)
        self.assertFalse(profile["center_blocked"])
        self.assertEqual(profile["open_side_turn"], "turn_right")

    def test_wall_escape_cycle_override_changes_forward_to_turn(self):
        tick, frame_b64 = self._load_tick("20260424_100519", 49)
        profile = main._frame_path_profile(
            frame_b64,
            tick["learned_floor_rgb"],
            tick["learned_wall_rgb"],
        )
        decision = {
            "action": "forward",
            "linear_speed": 0.25,
            "turn_degrees": 0.0,
            "duration_ms": 700,
            "confidence": 0.2,
            "reason": "The path ahead looks clear.",
            "comment_front": "",
            "comment_rear": "",
            "plan_of_action": "",
            "reasoning_steps": ["a", "b", "c", "d"],
        }

        overridden = main._apply_wall_escape_cycle_override(
            decision, profile, recent_wall_escape_count=3, max_turn_deg=90.0
        )

        self.assertEqual(overridden["action"], "turn_right")
        self.assertEqual(overridden["turn_degrees"], 65.0)
        self.assertIn("wall-escape-cycle-override", overridden["reason"])

    def test_persistent_wall_history_detects_repeated_forward_wall_view(self):
        current_tick, current_frame_b64 = self._load_tick("20260424_102552", 31)
        current_profile = main._frame_path_profile(
            current_frame_b64,
            current_tick["learned_floor_rgb"],
            current_tick["learned_wall_rgb"],
        )
        current_sig = main._build_nav_signature(
            current_tick["telemetry"]["orientation"],
            current_tick["front_uniformity"],
            current_tick["front_tb_delta"],
            current_tick["bot_dist_to_floor"],
            current_tick["bot_dist_to_wall"],
            current_profile,
        )

        history = []
        for tick_no in (28, 29, 30):
            tick, frame_b64 = self._load_tick("20260424_102552", tick_no)
            profile = main._frame_path_profile(
                frame_b64,
                tick["learned_floor_rgb"],
                tick["learned_wall_rgb"],
            )
            history.append(
                {
                    "action": "forward",
                    "nav_signature": main._build_nav_signature(
                        tick["telemetry"]["orientation"],
                        tick["front_uniformity"],
                        tick["front_tb_delta"],
                        tick["bot_dist_to_floor"],
                        tick["bot_dist_to_wall"],
                        profile,
                    ),
                }
            )

        forced_turn = main._detect_persistent_wall_ahead_turn(history, current_sig)

        self.assertEqual(forced_turn, "turn_right")

    def test_persistent_wall_override_changes_forward_to_turn(self):
        decision = {
            "action": "forward",
            "linear_speed": 0.25,
            "turn_degrees": 0.0,
            "duration_ms": 700,
            "confidence": 1.0,
            "reason": "The path ahead looks clear.",
            "comment_front": "",
            "comment_rear": "",
            "plan_of_action": "",
            "reasoning_steps": ["a", "b", "c", "d"],
        }

        overridden = main._apply_persistent_wall_ahead_override(
            decision, "turn_right", 90.0
        )

        self.assertEqual(overridden["action"], "turn_right")
        self.assertEqual(overridden["turn_degrees"], 55.0)
        self.assertIn("persistent-wall-ahead-override", overridden["reason"])

    def test_no_backward_policy_changes_backward_to_turn(self):
        tick, frame_b64 = self._load_tick("20260424_100519", 49)
        profile = main._frame_path_profile(
            frame_b64,
            tick["learned_floor_rgb"],
            tick["learned_wall_rgb"],
        )
        decision = {
            "action": "backward",
            "linear_speed": 0.15,
            "turn_degrees": 0.0,
            "duration_ms": 900,
            "confidence": 0.9,
            "reason": "Need to back up.",
            "comment_front": "",
            "comment_rear": "",
            "plan_of_action": "",
            "reasoning_steps": ["a", "b", "c", "d"],
        }

        overridden = main._apply_no_backward_policy(
            decision, profile, 90.0, last_turn_direction=None
        )

        self.assertEqual(overridden["action"], "turn_right")
        self.assertEqual(overridden["turn_degrees"], 70.0)
        self.assertIn("no-backward-policy", overridden["reason"])

    def test_choose_recovery_turn_falls_back_to_opposite_last_turn(self):
        action = main._choose_recovery_turn(
            path_profile={"preferred_turn": None, "open_side_turn": None},
            last_turn_direction="turn_left",
            preferred_turn=None,
        )
        self.assertEqual(action, "turn_right")

    def test_local_controller_returns_forward_for_obvious_clear_center(self):
        tick, frame_b64 = self._load_tick("20260424_102552", 29)
        profile = main._frame_path_profile(
            frame_b64,
            tick["learned_floor_rgb"],
            tick["learned_wall_rgb"],
        )
        decision = main._decide_from_local_controller(
            path_profile=profile,
            persistent_wall_turn=None,
            recent_wall_escape_count=0,
            color_sample_count=6,
            spin_detected=False,
            max_linear=0.25,
            max_turn_deg=90.0,
            max_forward_ms=3000,
            last_turn_direction=None,
            bot_dist_to_floor=tick["bot_dist_to_floor"],
            bot_dist_to_wall=tick["bot_dist_to_wall"],
            uniformity=tick["front_uniformity"],
            tb_delta=tick["front_tb_delta"],
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision["action"], "forward")

    def test_local_controller_uses_persistent_wall_turn_before_gemini(self):
        tick, frame_b64 = self._load_tick("20260424_102552", 31)
        profile = main._frame_path_profile(
            frame_b64,
            tick["learned_floor_rgb"],
            tick["learned_wall_rgb"],
        )
        decision = main._decide_from_local_controller(
            path_profile=profile,
            persistent_wall_turn="turn_right",
            recent_wall_escape_count=0,
            color_sample_count=6,
            spin_detected=False,
            max_linear=0.25,
            max_turn_deg=90.0,
            max_forward_ms=3000,
            last_turn_direction=None,
            bot_dist_to_floor=tick["bot_dist_to_floor"],
            bot_dist_to_wall=tick["bot_dist_to_wall"],
            uniformity=tick["front_uniformity"],
            tb_delta=tick["front_tb_delta"],
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision["action"], "turn_right")

    def test_local_controller_allows_forward_when_lane_reopens_after_spin(self):
        tick, frame_b64 = self._load_tick("20260427_103711", 22)
        profile = main._frame_path_profile(
            frame_b64,
            tick["learned_floor_rgb"],
            tick["learned_wall_rgb"],
        )
        decision = main._decide_from_local_controller(
            path_profile=profile,
            persistent_wall_turn=None,
            recent_wall_escape_count=tick["recent_wall_escape_count"],
            color_sample_count=tick["color_sample_count"],
            spin_detected=tick["spin_detected"],
            max_linear=0.25,
            max_turn_deg=90.0,
            max_forward_ms=3000,
            last_turn_direction="turn_left",
            bot_dist_to_floor=tick["bot_dist_to_floor"],
            bot_dist_to_wall=tick["bot_dist_to_wall"],
            uniformity=tick["front_uniformity"],
            tb_delta=tick["front_tb_delta"],
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision["action"], "forward")

    def test_side_opening_only_detector_flags_tick_44(self):
        tick, frame_b64 = self._load_tick("20260427_105735", 44)
        profile = main._frame_path_profile(
            frame_b64,
            tick["learned_floor_rgb"],
            tick["learned_wall_rgb"],
        )
        forced_turn = main._detect_side_opening_only_turn(
            path_profile=profile,
            bot_dist_to_floor=tick["bot_dist_to_floor"],
            bot_dist_to_wall=tick["bot_dist_to_wall"],
            uniformity=tick["front_uniformity"],
            tb_delta=tick["front_tb_delta"],
            color_sample_count=tick["color_sample_count"],
            recent_wall_escape_count=tick["recent_wall_escape_count"],
        )
        self.assertEqual(forced_turn, "turn_left")

    def test_local_controller_turns_when_only_side_opening_is_viable(self):
        tick, frame_b64 = self._load_tick("20260427_105735", 44)
        profile = main._frame_path_profile(
            frame_b64,
            tick["learned_floor_rgb"],
            tick["learned_wall_rgb"],
        )
        decision = main._decide_from_local_controller(
            path_profile=profile,
            persistent_wall_turn=None,
            recent_wall_escape_count=tick["recent_wall_escape_count"],
            color_sample_count=tick["color_sample_count"],
            spin_detected=tick["spin_detected"],
            max_linear=0.25,
            max_turn_deg=180.0,
            max_forward_ms=3000,
            last_turn_direction=None,
            bot_dist_to_floor=tick["bot_dist_to_floor"],
            bot_dist_to_wall=tick["bot_dist_to_wall"],
            uniformity=tick["front_uniformity"],
            tb_delta=tick["front_tb_delta"],
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision["action"], "turn_left")

    def test_side_opening_only_override_changes_forward_to_turn(self):
        tick, frame_b64 = self._load_tick("20260427_105735", 44)
        profile = main._frame_path_profile(
            frame_b64,
            tick["learned_floor_rgb"],
            tick["learned_wall_rgb"],
        )
        decision = {
            "action": "forward",
            "linear_speed": 0.25,
            "turn_degrees": 0.0,
            "duration_ms": 700,
            "confidence": 0.0,
            "reason": "Path is clear.",
            "comment_front": "",
            "comment_rear": "",
            "plan_of_action": "",
            "reasoning_steps": ["a", "b", "c", "d"],
        }

        overridden = main._apply_side_opening_only_override(
            decision,
            profile,
            tick["bot_dist_to_floor"],
            tick["bot_dist_to_wall"],
            tick["front_uniformity"],
            tick["front_tb_delta"],
            tick["color_sample_count"],
            tick["recent_wall_escape_count"],
            180.0,
            None,
        )

        self.assertEqual(overridden["action"], "turn_left")
        self.assertEqual(overridden["turn_degrees"], 60.0)
        self.assertIn("side-opening-only-override", overridden["reason"])

    def test_clear_corridor_does_not_trigger_side_opening_only_turn(self):
        tick, frame_b64 = self._load_tick("20260427_110920", 7)
        profile = main._frame_path_profile(
            frame_b64,
            tick["learned_floor_rgb"],
            tick["learned_wall_rgb"],
        )
        forced_turn = main._detect_side_opening_only_turn(
            path_profile=profile,
            bot_dist_to_floor=tick["bot_dist_to_floor"],
            bot_dist_to_wall=tick["bot_dist_to_wall"],
            uniformity=tick["front_uniformity"],
            tb_delta=tick["front_tb_delta"],
            color_sample_count=tick["color_sample_count"],
            recent_wall_escape_count=tick["recent_wall_escape_count"],
        )
        self.assertIsNone(forced_turn)

    def test_clear_corridor_forward_is_not_overridden_by_side_opening_rule(self):
        tick, frame_b64 = self._load_tick("20260427_110920", 7)
        profile = main._frame_path_profile(
            frame_b64,
            tick["learned_floor_rgb"],
            tick["learned_wall_rgb"],
        )
        decision = {
            "action": "forward",
            "linear_speed": 0.2,
            "turn_degrees": 0.0,
            "duration_ms": 700,
            "confidence": 0.0,
            "reason": "The immediate path is clear.",
            "comment_front": "",
            "comment_rear": "",
            "plan_of_action": "",
            "reasoning_steps": ["a", "b", "c", "d"],
        }

        overridden = main._apply_side_opening_only_override(
            decision,
            profile,
            tick["bot_dist_to_floor"],
            tick["bot_dist_to_wall"],
            tick["front_uniformity"],
            tick["front_tb_delta"],
            tick["color_sample_count"],
            tick["recent_wall_escape_count"],
            180.0,
            None,
        )

        self.assertEqual(overridden["action"], "forward")

    def test_spin_break_does_not_override_reopened_corridor(self):
        tick, frame_b64 = self._load_tick("20260427_111412", 36)
        profile = main._frame_path_profile(
            frame_b64,
            tick["learned_floor_rgb"],
            tick["learned_wall_rgb"],
        )
        decision = main._decide_from_local_controller(
            path_profile=profile,
            persistent_wall_turn=None,
            recent_wall_escape_count=tick["recent_wall_escape_count"],
            color_sample_count=tick["color_sample_count"],
            spin_detected=tick["spin_detected"],
            max_linear=0.25,
            max_turn_deg=180.0,
            max_forward_ms=3000,
            last_turn_direction="turn_right",
            bot_dist_to_floor=tick["bot_dist_to_floor"],
            bot_dist_to_wall=tick["bot_dist_to_wall"],
            uniformity=tick["front_uniformity"],
            tb_delta=tick["front_tb_delta"],
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision["action"], "forward")

    def test_early_uncertain_side_opening_turns_after_wall_escape(self):
        tick, frame_b64 = self._load_tick("20260427_113027", 3)
        profile = main._frame_path_profile(
            frame_b64,
            tick["learned_floor_rgb"],
            tick["learned_wall_rgb"],
        )
        forced_turn = main._detect_side_opening_only_turn(
            path_profile=profile,
            bot_dist_to_floor=tick["bot_dist_to_floor"],
            bot_dist_to_wall=tick["bot_dist_to_wall"],
            uniformity=tick["front_uniformity"],
            tb_delta=tick["front_tb_delta"],
            color_sample_count=tick["color_sample_count"],
            recent_wall_escape_count=tick["recent_wall_escape_count"],
        )
        self.assertEqual(forced_turn, "turn_right")

        decision = main._decide_from_local_controller(
            path_profile=profile,
            persistent_wall_turn=None,
            recent_wall_escape_count=tick["recent_wall_escape_count"],
            color_sample_count=tick["color_sample_count"],
            spin_detected=tick["spin_detected"],
            max_linear=0.25,
            max_turn_deg=180.0,
            max_forward_ms=3000,
            last_turn_direction="turn_right",
            bot_dist_to_floor=tick["bot_dist_to_floor"],
            bot_dist_to_wall=tick["bot_dist_to_wall"],
            uniformity=tick["front_uniformity"],
            tb_delta=tick["front_tb_delta"],
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision["action"], "turn_right")

    def test_wall_proximity_escape_does_not_fire_when_strong_forward_lane_is_visible(self):
        calib_tick, _ = self._load_tick("20260427_113704", 2)
        frame_b64 = self._load_frame_b64("20260427_113704", 3)
        color_sample = main._frame_color_samples(frame_b64)
        profile = main._frame_path_profile(
            frame_b64,
            calib_tick["learned_floor_rgb"],
            calib_tick["learned_wall_rgb"],
        )
        bot_dist_to_floor = main._rgb_distance(color_sample["bot_rgb"], calib_tick["learned_floor_rgb"])
        bot_dist_to_wall = main._rgb_distance(color_sample["bot_rgb"], calib_tick["learned_wall_rgb"])
        looks_like_wall_at_floor = (
            bot_dist_to_wall < bot_dist_to_floor - 15
            and bot_dist_to_floor > 25
        )

        self.assertTrue(
            main._has_clear_forward_lane(
                path_profile=profile,
                bot_dist_to_floor=bot_dist_to_floor,
                bot_dist_to_wall=bot_dist_to_wall,
                uniformity=main._frame_uniformity(frame_b64),
                tb_delta=main._frame_top_bottom_delta(frame_b64),
            )
        )
        self.assertFalse(
            main._is_pressed_against_wall(
                uniformity=main._frame_uniformity(frame_b64),
                tb_delta=main._frame_top_bottom_delta(frame_b64),
                looks_like_wall_at_floor=looks_like_wall_at_floor,
                path_profile=profile,
                bot_dist_to_floor=bot_dist_to_floor,
                bot_dist_to_wall=bot_dist_to_wall,
            )
        )

    def test_repeat_turn_escalation_uses_blocked_turn_staircase(self):
        decision = {
            "action": "turn_right",
            "linear_speed": 0.0,
            "turn_degrees": 80.0,
            "duration_ms": 800,
            "confidence": 1.0,
            "reason": "Need a committed right turn.",
            "comment_front": "",
            "comment_rear": "",
            "plan_of_action": "",
            "reasoning_steps": ["a", "b", "c", "d"],
        }

        first_turn = main._apply_repeat_turn_escalation(
            decision,
            max_turn_deg=180.0,
            last_turn_direction=None,
            consecutive_turns=0,
        )
        second_turn = main._apply_repeat_turn_escalation(
            decision,
            max_turn_deg=180.0,
            last_turn_direction="turn_right",
            consecutive_turns=1,
        )
        third_turn = main._apply_repeat_turn_escalation(
            decision,
            max_turn_deg=180.0,
            last_turn_direction="turn_right",
            consecutive_turns=2,
        )
        fourth_turn = main._apply_repeat_turn_escalation(
            decision,
            max_turn_deg=180.0,
            last_turn_direction="turn_right",
            consecutive_turns=3,
        )

        self.assertEqual(first_turn["turn_degrees"], 45.0)
        self.assertEqual(second_turn["turn_degrees"], 90.0)
        self.assertEqual(third_turn["turn_degrees"], 90.0)
        self.assertEqual(fourth_turn["turn_degrees"], 90.0)
        self.assertIn("turn-search-staircase", fourth_turn["reason"])

    def test_repeat_turn_escalation_respects_turn_cap(self):
        decision = {
            "action": "turn_left",
            "linear_speed": 0.0,
            "turn_degrees": 70.0,
            "duration_ms": 800,
            "confidence": 1.0,
            "reason": "Need a committed left turn.",
            "comment_front": "",
            "comment_rear": "",
            "plan_of_action": "",
            "reasoning_steps": ["a", "b", "c", "d"],
        }

        overridden = main._apply_repeat_turn_escalation(
            decision,
            max_turn_deg=120.0,
            last_turn_direction="turn_left",
            consecutive_turns=3,
        )

        self.assertEqual(overridden["turn_degrees"], 90.0)

    def test_bounded_turn_scan_progresses_45_then_90_on_same_side(self):
        decision = {
            "action": "turn_right",
            "linear_speed": 0.0,
            "turn_degrees": 180.0,
            "duration_ms": 800,
            "confidence": 1.0,
            "reason": "Need a right search turn.",
            "comment_front": "",
            "comment_rear": "",
            "plan_of_action": "",
            "reasoning_steps": ["a", "b", "c", "d"],
        }

        first_turn, scan_state = main._apply_bounded_turn_scan_policy(
            decision=decision,
            current_heading=180.0,
            scan_state=main._new_turn_scan_state(),
            max_turn_deg=180.0,
            path_profile=None,
        )
        second_turn, scan_state = main._apply_bounded_turn_scan_policy(
            decision=decision,
            current_heading=135.0,
            scan_state=scan_state,
            max_turn_deg=180.0,
            path_profile=None,
        )

        self.assertEqual(first_turn["action"], "turn_right")
        self.assertEqual(first_turn["turn_degrees"], 45.0)
        self.assertEqual(second_turn["action"], "turn_right")
        self.assertEqual(second_turn["turn_degrees"], 45.0)
        self.assertEqual(scan_state["direction"], "turn_right")
        self.assertEqual(scan_state["step_index"], 2)

    def test_bounded_turn_scan_switches_sides_after_90(self):
        decision = {
            "action": "turn_right",
            "linear_speed": 0.0,
            "turn_degrees": 180.0,
            "duration_ms": 800,
            "confidence": 1.0,
            "reason": "Need another search turn.",
            "comment_front": "",
            "comment_rear": "",
            "plan_of_action": "",
            "reasoning_steps": ["a", "b", "c", "d"],
        }

        _, scan_state = main._apply_bounded_turn_scan_policy(
            decision=decision,
            current_heading=180.0,
            scan_state=main._new_turn_scan_state(),
            max_turn_deg=180.0,
            path_profile=None,
        )
        _, scan_state = main._apply_bounded_turn_scan_policy(
            decision=decision,
            current_heading=135.0,
            scan_state=scan_state,
            max_turn_deg=180.0,
            path_profile=None,
        )
        switched_turn, scan_state = main._apply_bounded_turn_scan_policy(
            decision=decision,
            current_heading=90.0,
            scan_state=scan_state,
            max_turn_deg=180.0,
            path_profile=None,
        )

        self.assertEqual(switched_turn["action"], "turn_left")
        self.assertEqual(switched_turn["turn_degrees"], 135.0)
        self.assertEqual(scan_state["direction"], "turn_left")
        self.assertEqual(scan_state["step_index"], 1)
        self.assertTrue(scan_state["right_checked"])

    def test_bounded_turn_scan_clamps_large_turn_request_to_first_step(self):
        decision = {
            "action": "turn_left",
            "linear_speed": 0.0,
            "turn_degrees": 180.0,
            "duration_ms": 800,
            "confidence": 1.0,
            "reason": "Try a very large left turn.",
            "comment_front": "",
            "comment_rear": "",
            "plan_of_action": "",
            "reasoning_steps": ["a", "b", "c", "d"],
        }

        overridden, scan_state = main._apply_bounded_turn_scan_policy(
            decision=decision,
            current_heading=200.0,
            scan_state=main._new_turn_scan_state(),
            max_turn_deg=180.0,
            path_profile=None,
        )

        self.assertEqual(overridden["action"], "turn_left")
        self.assertEqual(overridden["turn_degrees"], 45.0)
        self.assertEqual(scan_state["step_index"], 1)

    def test_history_overrides_do_not_block_clear_forward_lane(self):
        tick, _ = self._load_tick("20260427_110920", 4)
        decision = {
            "action": "forward",
            "linear_speed": 0.25,
            "turn_degrees": 0.0,
            "duration_ms": 700,
            "confidence": 1.0,
            "reason": "Clear lane ahead.",
            "comment_front": "",
            "comment_rear": "",
            "plan_of_action": "",
            "reasoning_steps": ["a", "b", "c", "d"],
        }

        overridden = main._apply_history_forward_turn_overrides(
            decision=decision,
            path_profile=tick["path_profile"],
            recent_wall_escape_count=tick["recent_wall_escape_count"],
            persistent_wall_turn=tick["persistent_wall_turn"],
            max_turn_deg=180.0,
            clear_forward_lane=True,
        )

        self.assertEqual(overridden["action"], "forward")

    def test_history_overrides_turn_forward_when_lane_not_clear(self):
        tick, _ = self._load_tick("20260427_103711", 12)
        decision = {
            "action": "forward",
            "linear_speed": 0.25,
            "turn_degrees": 0.0,
            "duration_ms": 700,
            "confidence": 1.0,
            "reason": "Try forward.",
            "comment_front": "",
            "comment_rear": "",
            "plan_of_action": "",
            "reasoning_steps": ["a", "b", "c", "d"],
        }

        overridden = main._apply_history_forward_turn_overrides(
            decision=decision,
            path_profile=tick["path_profile"],
            recent_wall_escape_count=tick["recent_wall_escape_count"],
            persistent_wall_turn=tick["persistent_wall_turn"],
            max_turn_deg=180.0,
            clear_forward_lane=False,
        )

        self.assertEqual(overridden["action"], "turn_right")

    def test_repeated_flat_lane_surface_turns_instead_of_forward(self):
        history = []
        for tick_no in (10, 11, 12):
            tick, frame_b64 = self._load_tick("20260429_092039", tick_no)
            profile = main._frame_path_profile(
                frame_b64,
                tick["learned_floor_rgb"],
                tick["learned_wall_rgb"],
            )
            signature = main._build_nav_signature(
                tick["telemetry"]["orientation"],
                tick["front_uniformity"],
                tick["front_tb_delta"],
                tick["bot_dist_to_floor"],
                tick["bot_dist_to_wall"],
                profile,
            )
            history.append({"action": "forward", "nav_signature": signature})

        tick, frame_b64 = self._load_tick("20260429_092039", 13)
        profile = main._frame_path_profile(
            frame_b64,
            tick["learned_floor_rgb"],
            tick["learned_wall_rgb"],
        )
        current_signature = main._build_nav_signature(
            tick["telemetry"]["orientation"],
            tick["front_uniformity"],
            tick["front_tb_delta"],
            tick["bot_dist_to_floor"],
            tick["bot_dist_to_wall"],
            profile,
        )

        self.assertTrue(current_signature["flat_lane_surface"])
        self.assertEqual(
            main._detect_persistent_wall_ahead_turn(history, current_signature),
            "turn_right",
        )

        decision = {
            "action": "forward",
            "linear_speed": 0.25,
            "turn_degrees": 0.0,
            "duration_ms": 700,
            "confidence": 1.0,
            "reason": "Try forward.",
            "comment_front": "",
            "comment_rear": "",
            "plan_of_action": "",
            "reasoning_steps": ["a", "b", "c", "d"],
        }
        overridden = main._apply_history_forward_turn_overrides(
            decision=decision,
            path_profile=profile,
            recent_wall_escape_count=0,
            persistent_wall_turn="turn_right",
            max_turn_deg=180.0,
            clear_forward_lane=False,
        )

        self.assertEqual(overridden["action"], "turn_right")

    def test_repeated_flat_lane_surface_without_side_bias_turns_instead_of_forward(self):
        history = []
        for tick_no in (24, 25, 26):
            tick, frame_b64 = self._load_tick("20260429_093515", tick_no)
            profile = main._frame_path_profile(
                frame_b64,
                tick["learned_floor_rgb"],
                tick["learned_wall_rgb"],
            )
            signature = main._build_nav_signature(
                tick["telemetry"]["orientation"],
                tick["front_uniformity"],
                tick["front_tb_delta"],
                tick["bot_dist_to_floor"],
                tick["bot_dist_to_wall"],
                profile,
            )
            history.append({"action": "forward", "nav_signature": signature})

        tick, frame_b64 = self._load_tick("20260429_093515", 27)
        profile = main._frame_path_profile(
            frame_b64,
            tick["learned_floor_rgb"],
            tick["learned_wall_rgb"],
        )
        current_signature = main._build_nav_signature(
            tick["telemetry"]["orientation"],
            tick["front_uniformity"],
            tick["front_tb_delta"],
            tick["bot_dist_to_floor"],
            tick["bot_dist_to_wall"],
            profile,
        )

        self.assertTrue(current_signature["flat_lane_surface"])
        self.assertIsNone(current_signature["open_side_turn"])
        self.assertEqual(
            main._detect_persistent_wall_ahead_turn(history, current_signature),
            "turn_right",
        )

    def test_repeated_smooth_diagonal_obstruction_turns_instead_of_forward(self):
        history = []
        for tick_no in (10, 11, 12):
            tick, frame_b64 = self._load_tick("20260429_095453", tick_no)
            profile = main._frame_path_profile(
                frame_b64,
                tick["learned_floor_rgb"],
                tick["learned_wall_rgb"],
            )
            signature = main._build_nav_signature(
                tick["telemetry"]["orientation"],
                tick["front_uniformity"],
                tick["front_tb_delta"],
                tick["bot_dist_to_floor"],
                tick["bot_dist_to_wall"],
                profile,
            )
            history.append({"action": "forward", "nav_signature": signature})

        tick, frame_b64 = self._load_tick("20260429_095453", 13)
        profile = main._frame_path_profile(
            frame_b64,
            tick["learned_floor_rgb"],
            tick["learned_wall_rgb"],
        )
        current_signature = main._build_nav_signature(
            tick["telemetry"]["orientation"],
            tick["front_uniformity"],
            tick["front_tb_delta"],
            tick["bot_dist_to_floor"],
            tick["bot_dist_to_wall"],
            profile,
        )

        self.assertTrue(current_signature["smooth_diagonal_obstruction"])
        self.assertEqual(
            main._detect_persistent_wall_ahead_turn(history, current_signature),
            "turn_left",
        )

    def test_repeated_textured_obstruction_turns_instead_of_forward(self):
        history = []
        for tick_no in (3, 4, 5, 6):
            tick, frame_b64 = self._load_tick("20260429_101634", tick_no)
            profile = main._frame_path_profile(
                frame_b64,
                tick["learned_floor_rgb"],
                tick["learned_wall_rgb"],
            )
            signature = main._build_nav_signature(
                tick["telemetry"]["orientation"],
                tick["front_uniformity"],
                tick["front_tb_delta"],
                tick["bot_dist_to_floor"],
                tick["bot_dist_to_wall"],
                profile,
            )
            history.append({"action": "forward", "nav_signature": signature})

        tick, frame_b64 = self._load_tick("20260429_101634", 7)
        profile = main._frame_path_profile(
            frame_b64,
            tick["learned_floor_rgb"],
            tick["learned_wall_rgb"],
        )
        current_signature = main._build_nav_signature(
            tick["telemetry"]["orientation"],
            tick["front_uniformity"],
            tick["front_tb_delta"],
            tick["bot_dist_to_floor"],
            tick["bot_dist_to_wall"],
            profile,
        )

        self.assertTrue(current_signature["textured_lane_obstruction"])
        self.assertEqual(
            main._detect_persistent_wall_ahead_turn(history, current_signature),
            "turn_right",
        )

        decision = {
            "action": "forward",
            "linear_speed": 0.25,
            "turn_degrees": 0.0,
            "duration_ms": 700,
            "confidence": 1.0,
            "reason": "Looks like floor.",
            "comment_front": "",
            "comment_rear": "",
            "plan_of_action": "",
            "reasoning_steps": ["a", "b", "c", "d"],
        }
        overridden = main._apply_history_forward_turn_overrides(
            decision=decision,
            path_profile=profile,
            recent_wall_escape_count=0,
            persistent_wall_turn="turn_right",
            max_turn_deg=180.0,
            clear_forward_lane=False,
        )

        self.assertEqual(overridden["action"], "turn_right")

    def test_textured_obstruction_is_not_clear_forward_lane(self):
        tick, frame_b64 = self._load_tick("20260429_103041", 10)
        profile = main._frame_path_profile(
            frame_b64,
            tick["learned_floor_rgb"],
            tick["learned_wall_rgb"],
        )

        self.assertTrue(main._is_textured_lane_obstruction(profile))
        self.assertFalse(
            main._has_clear_forward_lane(
                path_profile=profile,
                bot_dist_to_floor=tick["bot_dist_to_floor"],
                bot_dist_to_wall=tick["bot_dist_to_wall"],
                uniformity=tick["front_uniformity"],
                tb_delta=tick["front_tb_delta"],
            )
        )

        gemini_turn = {
            "action": "turn_right",
            "linear_speed": 0.0,
            "turn_degrees": 45.0,
            "duration_ms": 800,
            "confidence": 1.0,
            "reason": "Box blocks the path.",
            "comment_front": "",
            "comment_rear": "",
            "plan_of_action": "",
            "reasoning_steps": ["a", "b", "c", "d"],
        }
        preserved = main._apply_visual_forward_override(
            decision=gemini_turn,
            clear_forward_lane=False,
            max_linear=0.25,
            max_forward_ms=3000,
        )

        self.assertEqual(preserved["action"], "turn_right")

    def test_smooth_close_surface_turns_instead_of_forward(self):
        tick, frame_b64 = self._load_tick("20260429_103935", 14)
        profile = main._frame_path_profile(
            frame_b64,
            tick["learned_floor_rgb"],
            tick["learned_wall_rgb"],
        )

        self.assertTrue(
            main._is_smooth_close_surface_obstruction(
                profile,
                tick["bot_dist_to_floor"],
            )
        )
        self.assertFalse(
            main._has_clear_forward_lane(
                path_profile=profile,
                bot_dist_to_floor=tick["bot_dist_to_floor"],
                bot_dist_to_wall=tick["bot_dist_to_wall"],
                uniformity=tick["front_uniformity"],
                tb_delta=tick["front_tb_delta"],
            )
        )

        decision = {
            "action": "forward",
            "linear_speed": 0.25,
            "turn_degrees": 0.0,
            "duration_ms": 700,
            "confidence": 1.0,
            "reason": "Looks open.",
            "comment_front": "",
            "comment_rear": "",
            "plan_of_action": "",
            "reasoning_steps": ["a", "b", "c", "d"],
        }
        overridden = main._apply_smooth_close_surface_override(
            decision=decision,
            path_profile=profile,
            bot_dist_to_floor=tick["bot_dist_to_floor"],
            max_turn_deg=180.0,
            last_turn_direction=None,
        )

        self.assertEqual(overridden["action"], "turn_right")
        self.assertEqual(overridden["turn_degrees"], 45.0)

    def test_flat_floor_colored_box_face_turns_instead_of_forward(self):
        tick, frame_b64 = self._load_tick("20260429_111242", 4)
        profile = main._frame_path_profile(
            frame_b64,
            tick["learned_floor_rgb"],
            tick["learned_wall_rgb"],
        )

        self.assertTrue(profile["center_blocked"] is False)
        self.assertTrue(profile["lane_std"] <= 8.5)
        self.assertTrue(main._is_flat_lane_surface(profile))
        self.assertTrue(
            main._is_smooth_close_surface_obstruction(
                profile,
                tick["bot_dist_to_floor"],
            )
        )
        self.assertFalse(
            main._has_clear_forward_lane(
                path_profile=profile,
                bot_dist_to_floor=tick["bot_dist_to_floor"],
                bot_dist_to_wall=tick["bot_dist_to_wall"],
                uniformity=tick["front_uniformity"],
                tb_delta=tick["front_tb_delta"],
            )
        )

        decision = {
            "action": "forward",
            "linear_speed": 0.25,
            "turn_degrees": 0.0,
            "duration_ms": 700,
            "confidence": 1.0,
            "reason": "Looks like a low object.",
            "comment_front": "",
            "comment_rear": "",
            "plan_of_action": "",
            "reasoning_steps": ["a", "b", "c", "d"],
        }
        overridden = main._apply_smooth_close_surface_override(
            decision=decision,
            path_profile=profile,
            bot_dist_to_floor=tick["bot_dist_to_floor"],
            max_turn_deg=180.0,
            last_turn_direction=None,
        )

        self.assertEqual(overridden["action"], "turn_right")
        self.assertEqual(overridden["turn_degrees"], 45.0)

    def test_dark_center_floor_between_side_obstacles_stays_forward(self):
        tick, frame_b64 = self._load_tick("20260429_113821", 3)
        profile = main._frame_path_profile(
            frame_b64,
            tick["learned_floor_rgb"],
            tick["learned_wall_rgb"],
        )

        self.assertFalse(profile["center_blocked"])
        self.assertTrue(
            main._has_clear_forward_lane(
                path_profile=profile,
                bot_dist_to_floor=tick["bot_dist_to_floor"],
                bot_dist_to_wall=tick["bot_dist_to_wall"],
                uniformity=tick["front_uniformity"],
                tb_delta=tick["front_tb_delta"],
            )
        )

        gemini_turn = {
            "action": "turn_right",
            "linear_speed": 0.0,
            "turn_degrees": 45.0,
            "duration_ms": 800,
            "confidence": 0.0,
            "reason": "Mistaken side obstacle.",
            "comment_front": "",
            "comment_rear": "",
            "plan_of_action": "",
            "reasoning_steps": ["a", "b", "c", "d"],
        }
        overridden = main._apply_visual_forward_override(
            decision=gemini_turn,
            clear_forward_lane=True,
            max_linear=0.25,
            max_forward_ms=3000,
        )

        self.assertEqual(overridden["action"], "forward")

    def test_floor_runway_before_far_wall_stays_forward(self):
        tick, frame_b64 = self._load_tick("20260429_121630", 2)
        profile = main._frame_path_profile(
            frame_b64,
            tick["learned_floor_rgb"],
            tick["learned_wall_rgb"],
        )

        self.assertFalse(profile["center_blocked"])
        self.assertTrue(
            main._has_clear_forward_lane(
                path_profile=profile,
                bot_dist_to_floor=tick["bot_dist_to_floor"],
                bot_dist_to_wall=tick["bot_dist_to_wall"],
                uniformity=tick["front_uniformity"],
                tb_delta=tick["front_tb_delta"],
            )
        )

        gemini_turn = {
            "action": "turn_left",
            "linear_speed": 0.0,
            "turn_degrees": 45.0,
            "duration_ms": 800,
            "confidence": 0.0,
            "reason": "Mistaken far wall.",
            "comment_front": "",
            "comment_rear": "",
            "plan_of_action": "",
            "reasoning_steps": ["a", "b", "c", "d"],
        }
        overridden = main._apply_visual_forward_override(
            decision=gemini_turn,
            clear_forward_lane=True,
            max_linear=0.25,
            max_forward_ms=3000,
        )

        self.assertEqual(overridden["action"], "forward")

    def test_initial_floor_baseline_prevents_wall_drift_forward(self):
        run_id = "20260429_122311"
        state = {
            "floor_rgb": None,
            "wall_rgb": None,
            "initial_floor_rgb": None,
            "initial_wall_rgb": None,
            "color_sample_count": 0,
        }
        target = None

        for tick_no in range(1, 8):
            tick, frame_b64 = self._load_tick(run_id, tick_no)
            sample = main._frame_color_samples(frame_b64)
            tb_delta = main._frame_top_bottom_delta(frame_b64)
            profile = main._frame_path_profile(
                frame_b64,
                state["floor_rgb"],
                state["wall_rgb"],
            )
            dist_to_initial = main._rgb_distance(
                sample["bot_rgb"] if sample else None,
                state["initial_floor_rgb"],
            )
            floor_matches_initial = state["initial_floor_rgb"] is None or (
                dist_to_initial is not None and dist_to_initial <= 35.0
            )
            pre_bdf = main._rgb_distance(
                sample["bot_rgb"] if sample else None,
                state["floor_rgb"],
            )
            calibration_safe = not (
                profile
                and (
                    profile.get("center_blocked")
                    or main._is_flat_lane_surface(profile)
                    or main._is_textured_lane_obstruction(profile)
                    or main._is_smooth_close_surface_obstruction(profile, pre_bdf)
                )
            )
            should_update = (
                sample
                and tb_delta is not None
                and tb_delta >= 12.0
                and floor_matches_initial
                and calibration_safe
                and state["color_sample_count"] < 6
            )
            if should_update:
                count = state["color_sample_count"]
                if state["floor_rgb"] is None:
                    state["floor_rgb"] = list(sample["bot_rgb"])
                    state["wall_rgb"] = list(sample["top_rgb"])
                    state["initial_floor_rgb"] = list(sample["bot_rgb"])
                    state["initial_wall_rgb"] = list(sample["top_rgb"])
                else:
                    state["floor_rgb"] = [
                        round((state["floor_rgb"][i] * count + sample["bot_rgb"][i]) / (count + 1), 1)
                        for i in range(3)
                    ]
                    state["wall_rgb"] = [
                        round((state["wall_rgb"][i] * count + sample["top_rgb"][i]) / (count + 1), 1)
                        for i in range(3)
                    ]
                state["color_sample_count"] = count + 1

            if tick_no == 7:
                target = (tick, frame_b64, sample, tb_delta)

        self.assertIsNotNone(target)
        tick, frame_b64, sample, tb_delta = target
        profile = main._frame_path_profile(
            frame_b64,
            state["floor_rgb"],
            state["wall_rgb"],
        )
        bot_dist_to_floor = main._min_rgb_distance(
            sample["bot_rgb"],
            state["floor_rgb"],
            state["initial_floor_rgb"],
        )
        bot_dist_to_wall = main._min_rgb_distance(
            sample["bot_rgb"],
            state["wall_rgb"],
            state["initial_wall_rgb"],
        )

        self.assertEqual(state["color_sample_count"], 2)
        self.assertFalse(
            main._has_clear_forward_lane(
                path_profile=profile,
                bot_dist_to_floor=bot_dist_to_floor,
                bot_dist_to_wall=bot_dist_to_wall,
                uniformity=tick["front_uniformity"],
                tb_delta=tb_delta,
            )
        )
        self.assertEqual(
            main._detect_side_opening_only_turn(
                path_profile=profile,
                bot_dist_to_floor=bot_dist_to_floor,
                bot_dist_to_wall=bot_dist_to_wall,
                uniformity=tick["front_uniformity"],
                tb_delta=tb_delta,
                color_sample_count=state["color_sample_count"],
                recent_wall_escape_count=0,
            ),
            "turn_right",
        )

    def test_near_wall_floor_runway_uses_cautious_forward_probe(self):
        tick, frame_b64 = self._load_tick("20260429_130510", 3)
        profile = main._frame_path_profile(
            frame_b64,
            tick["learned_floor_rgb"],
            tick["learned_wall_rgb"],
        )

        self.assertTrue(
            main._is_cautious_forward_probe_lane(
                path_profile=profile,
                bot_dist_to_floor=tick["bot_dist_to_floor"],
                bot_dist_to_wall=tick["bot_dist_to_wall"],
                uniformity=tick["front_uniformity"],
                tb_delta=tick["front_tb_delta"],
            )
        )

        gemini_turn = {
            "action": "turn_left",
            "linear_speed": 0.0,
            "turn_degrees": 45.0,
            "duration_ms": 800,
            "confidence": 0.0,
            "reason": "Corner ahead.",
            "comment_front": "",
            "comment_rear": "",
            "plan_of_action": "",
            "reasoning_steps": ["a", "b", "c", "d"],
        }
        overridden = main._apply_cautious_forward_probe_override(
            decision=gemini_turn,
            path_profile=profile,
            bot_dist_to_floor=tick["bot_dist_to_floor"],
            bot_dist_to_wall=tick["bot_dist_to_wall"],
            uniformity=tick["front_uniformity"],
            tb_delta=tick["front_tb_delta"],
            max_linear=0.25,
            max_forward_ms=3000,
        )

        self.assertEqual(overridden["action"], "forward")
        self.assertEqual(overridden["linear_speed"], 0.15)
        self.assertEqual(overridden["duration_ms"], 400)

    def test_dark_uncertain_lane_turns_instead_of_forward(self):
        tick, frame_b64 = self._load_tick("20260429_100823", 20)
        profile = main._frame_path_profile(
            frame_b64,
            tick["learned_floor_rgb"],
            tick["learned_wall_rgb"],
        )

        self.assertTrue(
            main._is_dark_uncertain_lane(
                path_profile=profile,
                bot_dist_to_floor=tick["bot_dist_to_floor"],
                bot_dist_to_wall=tick["bot_dist_to_wall"],
                uniformity=tick["front_uniformity"],
            )
        )

        decision = {
            "action": "forward",
            "linear_speed": 0.25,
            "turn_degrees": 0.0,
            "duration_ms": 700,
            "confidence": 1.0,
            "reason": "Try forward.",
            "comment_front": "",
            "comment_rear": "",
            "plan_of_action": "",
            "reasoning_steps": ["a", "b", "c", "d"],
        }
        overridden = main._apply_dark_uncertain_lane_override(
            decision=decision,
            path_profile=profile,
            bot_dist_to_floor=tick["bot_dist_to_floor"],
            bot_dist_to_wall=tick["bot_dist_to_wall"],
            uniformity=tick["front_uniformity"],
            max_turn_deg=180.0,
            last_turn_direction=None,
        )

        self.assertEqual(overridden["action"], "turn_right")

    def test_turn_commitment_override_prevents_immediate_opposite_turn(self):
        history = [
            {
                "tick": 22,
                "action": "turn_left",
                "turn_degrees": 45.0,
            }
        ]
        decision = {
            "action": "turn_right",
            "linear_speed": 0.0,
            "turn_degrees": 45.0,
            "duration_ms": 800,
            "confidence": 1.0,
            "reason": "Need to turn right.",
            "comment_front": "",
            "comment_rear": "",
            "plan_of_action": "",
            "reasoning_steps": ["a", "b", "c", "d"],
        }

        overridden = main._apply_turn_commitment_override(
            decision=decision,
            history=history,
            clear_forward_lane=False,
        )

        self.assertEqual(overridden["action"], "turn_left")
        self.assertIn("turn-commitment", overridden["reason"])

    def test_turn_commitment_override_does_not_block_clear_forward_turn_choice(self):
        history = [
            {
                "tick": 22,
                "action": "turn_left",
                "turn_degrees": 45.0,
            }
        ]
        decision = {
            "action": "turn_right",
            "linear_speed": 0.0,
            "turn_degrees": 45.0,
            "duration_ms": 800,
            "confidence": 1.0,
            "reason": "Need to turn right.",
            "comment_front": "",
            "comment_rear": "",
            "plan_of_action": "",
            "reasoning_steps": ["a", "b", "c", "d"],
        }

        overridden = main._apply_turn_commitment_override(
            decision=decision,
            history=history,
            clear_forward_lane=True,
        )

        self.assertEqual(overridden["action"], "turn_right")

    def test_visual_corridor_counts_as_clear_forward_even_with_color_drift(self):
        tick, _ = self._load_tick("20260427_125040", 28)

        self.assertTrue(
            main._has_clear_forward_lane(
                path_profile=tick["path_profile"],
                bot_dist_to_floor=tick["bot_dist_to_floor"],
                bot_dist_to_wall=tick["bot_dist_to_wall"],
                uniformity=tick["front_uniformity"],
                tb_delta=tick["front_tb_delta"],
            )
        )

    def test_visual_forward_override_changes_turn_to_forward(self):
        decision = {
            "action": "turn_right",
            "linear_speed": 0.0,
            "turn_degrees": 45.0,
            "duration_ms": 800,
            "confidence": 1.0,
            "reason": "Need to turn right.",
            "comment_front": "",
            "comment_rear": "",
            "plan_of_action": "",
            "reasoning_steps": ["a", "b", "c", "d"],
        }

        overridden = main._apply_visual_forward_override(
            decision=decision,
            clear_forward_lane=True,
            max_linear=0.25,
            max_forward_ms=3000,
        )

        self.assertEqual(overridden["action"], "forward")

    def test_visual_forward_override_allows_mostly_side_obstacle(self):
        decision = {
            "action": "turn_left",
            "linear_speed": 0.0,
            "turn_degrees": 45.0,
            "duration_ms": 800,
            "confidence": 0.9,
            "reason": "The path appears blocked by a large cardboard box mostly on the right side.",
            "comment_front": "The box is mostly on my right, with clear floor still ahead.",
            "comment_rear": "",
            "plan_of_action": "",
            "reasoning_steps": ["a", "b", "c", "d"],
        }

        overridden = main._apply_visual_forward_override(
            decision=decision,
            clear_forward_lane=True,
            max_linear=0.25,
            max_forward_ms=3000,
        )

        self.assertEqual(overridden["action"], "forward")

    def test_visual_forward_override_still_trusts_center_obstacle(self):
        decision = {
            "action": "turn_left",
            "linear_speed": 0.0,
            "turn_degrees": 45.0,
            "duration_ms": 800,
            "confidence": 0.9,
            "reason": "A box is directly in the center lane.",
            "comment_front": "The box blocks my forward path.",
            "comment_rear": "",
            "plan_of_action": "",
            "reasoning_steps": ["a", "b", "c", "d"],
        }

        overridden = main._apply_visual_forward_override(
            decision=decision,
            clear_forward_lane=True,
            max_linear=0.25,
            max_forward_ms=3000,
        )

        self.assertEqual(overridden["action"], "turn_left")

    def test_side_opening_override_preserves_confident_clear_center_forward(self):
        path_profile = {
            "center_blocked": False,
            "preferred_turn": None,
            "open_side_turn": "turn_left",
            "left_floor_dist": 5.7,
            "left_wall_dist": 101.5,
            "center_floor_dist": 22.9,
            "center_wall_dist": 73.3,
            "right_floor_dist": 102.1,
            "right_wall_dist": 8.4,
            "lane_std": 24.5,
            "lane_tb_delta": 18.7,
            "lane_edge_density": 0.0439,
            "side_std_gap": -9.1,
        }
        decision = {
            "action": "forward",
            "linear_speed": 0.25,
            "turn_degrees": 0.0,
            "duration_ms": 700,
            "confidence": 1.0,
            "reason": "The path directly in front of me appears clear and open.",
            "comment_front": "The driving lane ahead is clear, with boxes off to my left and a wall on my right.",
            "comment_rear": "",
            "plan_of_action": "",
            "reasoning_steps": [
                "I am comparing the current frame to the references.",
                "The current image shows an open path directly in front of me, with clear floor visible.",
                "The boxes are off to the left side rather than in the center lane.",
                "Therefore, moving forward is the appropriate action.",
            ],
        }

        overridden = main._apply_side_opening_only_override(
            decision=decision,
            path_profile=path_profile,
            bot_dist_to_floor=38.3,
            bot_dist_to_wall=56.4,
            uniformity=35.3,
            tb_delta=26.0,
            color_sample_count=6,
            recent_wall_escape_count=0,
            max_turn_deg=180.0,
            last_turn_direction="turn_right",
        )

        self.assertEqual(overridden["action"], "forward")

    def test_side_opening_override_preserves_low_confidence_forward_probe(self):
        path_profile = {
            "center_blocked": False,
            "preferred_turn": None,
            "open_side_turn": "turn_right",
            "left_floor_dist": 45.9,
            "left_wall_dist": 89.8,
            "center_floor_dist": 54.0,
            "center_wall_dist": 113.6,
            "right_floor_dist": 29.3,
            "right_wall_dist": 92.4,
            "lane_std": 14.8,
            "lane_tb_delta": 9.6,
            "lane_edge_density": 0.1079,
            "side_std_gap": 11.8,
        }
        turn_decision = {
            "action": "turn_left",
            "linear_speed": 0.0,
            "turn_degrees": 45.0,
            "duration_ms": 800,
            "confidence": 0.0,
            "reason": "A box blocks the path.",
            "comment_front": "",
            "comment_rear": "",
            "plan_of_action": "",
            "reasoning_steps": ["a", "b", "c", "d"],
        }

        probe = main._apply_low_confidence_floor_probe_override(
            decision=turn_decision,
            bot_dist_to_floor=28.4,
            tb_delta=36.0,
            path_profile=path_profile,
            max_linear=0.25,
            uniformity=34.4,
            learned_floor_rgb=[92.7, 92.7, 93.9],
            learned_wall_rgb=[142.8, 143.0, 143.7],
            color_sample_count=6,
            recent_turn_count=3,
        )
        overridden = main._apply_side_opening_only_override(
            decision=probe,
            path_profile=path_profile,
            bot_dist_to_floor=28.4,
            bot_dist_to_wall=80.3,
            uniformity=34.4,
            tb_delta=36.0,
            color_sample_count=6,
            recent_wall_escape_count=0,
            max_turn_deg=180.0,
            last_turn_direction="turn_right",
        )

        self.assertEqual(probe["action"], "forward")
        self.assertIn("low-confidence floor-probe", probe["reason"])
        self.assertEqual(overridden["action"], "forward")

    def test_obstacle_keyword_detection_handles_punctuation(self):
        decision = {
            "reason": "There is a box, directly in my lane.",
            "comment_front": "",
            "reasoning_steps": [],
        }

        self.assertTrue(main._gemini_cites_specific_obstacle(decision))

    def test_narrow_gap_detector_ignores_negated_narrow_gap(self):
        decision = {
            "action": "forward",
            "reason": "The immediate path is clear.",
            "comment_front": "Objects are on my left and right sides.",
            "reasoning_steps": [
                "The center-bottom shows clear floor.",
                "There is a box on the left and a person on the right.",
                "The image does not resemble too-narrow gap because the center lane is clear.",
                "The bottom-center driving lane is clear.",
            ],
        }

        self.assertFalse(main._gemini_warns_narrow_gap(decision))

    def test_narrow_gap_detector_allows_safe_narrow_corridor(self):
        decision = {
            "action": "forward",
            "reason": (
                "The immediate path in front of me is clear and shows open floor. "
                "There are walls on both sides, forming a narrow corridor, but "
                "they do not block my immediate path forward."
            ),
            "comment_front": (
                "The immediate area directly in front of me is clear, with a "
                "visible floor extending forward. Walls are present on both sides, "
                "typical of a corridor."
            ),
            "reasoning_steps": [
                "The bottom-center of the image clearly shows open floor.",
                "Walls are visible on both sides, but they do not encroach upon my immediate driving lane.",
                "The overall perspective indicates a narrow corridor that is safe to proceed through.",
                "Telemetry suggests the center is not blocked.",
            ],
        }

        self.assertFalse(main._gemini_cites_specific_obstacle(decision))
        self.assertFalse(main._gemini_warns_narrow_gap(decision))


if __name__ == "__main__":
    unittest.main()
