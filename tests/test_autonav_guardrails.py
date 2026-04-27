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
        self.assertEqual(third_turn["turn_degrees"], 135.0)
        self.assertEqual(fourth_turn["turn_degrees"], 180.0)
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

        self.assertEqual(overridden["turn_degrees"], 120.0)

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


if __name__ == "__main__":
    unittest.main()
