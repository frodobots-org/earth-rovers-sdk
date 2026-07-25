from __future__ import annotations

import random

import numpy as np


def _sample_unique(population: list[tuple[int, int]], k: int, rng: random.Random) -> list[tuple[int, int]]:
    if k <= 0 or not population:
        return []
    return rng.sample(population, k=min(int(k), len(population)))


def sample_goals(
    num_goals: int = 100,
    grid_size: int = 240,
    rng: random.Random | None = None,
) -> list[tuple[int, int]]:
    """Sample goals on the top, left, and right edges of the planner grid."""
    rng = rng or random
    num_goals_edge = int((0.75 / 2.5) * int(num_goals))
    num_goals_top = int(num_goals) - 2 * num_goals_edge

    top_possible = [(0, col) for col in range(int(grid_size))]
    top_goals = _sample_unique(top_possible, num_goals_top, rng)

    max_side_row = max(0, min(int(grid_size) - 1, 179))
    valid_rows = list(range(0, max_side_row + 1))
    left_possible = [(row, 0) for row in valid_rows]
    right_possible = [(row, int(grid_size) - 1) for row in valid_rows]
    return top_goals + _sample_unique(left_possible, num_goals_edge, rng) + _sample_unique(
        right_possible, num_goals_edge, rng
    )


def sample_mid_for_goal(
    goal: tuple[int, int],
    robot: tuple[int, int],
    grid_size: int,
    rng: random.Random | None = None,
) -> tuple[int, int]:
    """Sample a midpoint for a quadratic path from robot to goal."""
    rng = rng or random
    gr, gc = int(goal[0]), int(goal[1])
    robot_row, robot_col = int(robot[0]), int(robot[1])
    grid_size = int(grid_size)

    if gr == 0:
        goal_col = gc
        mid_col = grid_size // 2
        left_bound = min(goal_col, mid_col)
        right_bound = max(goal_col, mid_col)
        rect_points = [(r, c) for r in range(0, grid_size) for c in range(left_bound, right_bound + 1)]
        slope = (robot_row - gr) / float(robot_col - goal_col) if robot_col != goal_col else float("inf")
        tri1 = [(r, c) for (r, c) in rect_points if r <= gr + slope * (c - goal_col)]
        tri2 = [(r, c) for (r, c) in rect_points if r >= gr + slope * (c - goal_col)]
        chosen = tri1 if rng.random() < 0.5 else tri2
        return rng.choice(chosen if chosen else rect_points)

    if gc == 0:
        rect_points = [(r, c) for r in range(gr, grid_size) for c in range(0, robot_col + 1)]
        slope = (robot_row - gr) / float(robot_col) if robot_col != 0 else float("inf")
        tri1 = [(r, c) for (r, c) in rect_points if r <= gr + slope * c]
        tri2 = [(r, c) for (r, c) in rect_points if r >= gr + slope * c]
        chosen = tri1 if rng.random() < 0.5 else tri2
        return rng.choice(chosen if chosen else rect_points)

    if gc == grid_size - 1:
        mirrored_goal = (gr, 0)
        mirrored_robot = (robot_row, grid_size - 1 - robot_col)
        mid_mir = sample_mid_for_goal(mirrored_goal, mirrored_robot, grid_size, rng=rng)
        return mid_mir[0], grid_size - 1 - mid_mir[1]

    if rng.random() <= 0.05:
        row_i = 0 if gr == 0 else rng.randint(0, max(0, gr))
    elif gr == grid_size - 1:
        row_i = max(0, grid_size - 1 - 150)
    else:
        row_i = rng.randint(gr, int(gr + (grid_size - gr) / 2))
    return row_i, rng.randint(0, grid_size - 1)


def quadratic_path(
    start: tuple[int, int],
    mid: tuple[int, int],
    goal: tuple[int, int],
    num_samples: int = 50,
) -> np.ndarray:
    (r0, c0) = start
    (rm, cm) = mid
    (r1, c1) = goal

    a0 = r0
    a1 = 4 * rm - 3 * r0 - r1
    a2 = (r1 - r0) - a1
    b0 = c0
    b1 = 4 * cm - 3 * c0 - c1
    b2 = (c1 - c0) - b1

    t = np.linspace(0.0, 1.0, int(num_samples) + 1, dtype=np.float64)
    rows = a0 + a1 * t + a2 * (t**2)
    cols = b0 + b1 * t + b2 * (t**2)
    return np.stack([rows, cols], axis=1)


def compute_arc_length(path: np.ndarray) -> np.ndarray:
    diffs = np.diff(path, axis=0)
    segment_lengths = np.linalg.norm(diffs, axis=1)
    return np.insert(np.cumsum(segment_lengths), 0, 0)


def uniformly_sample_by_arclength(
    start: tuple[int, int],
    mid: tuple[int, int],
    goal: tuple[int, int],
    num_points: int = 50,
    high_res: int = 1000,
) -> np.ndarray:
    high_res_path = quadratic_path(start, mid, goal, num_samples=high_res)
    cum_arc_length = compute_arc_length(high_res_path)
    total_length = float(cum_arc_length[-1])
    if total_length <= 1e-8:
        return np.repeat(np.asarray(start, dtype=np.float64)[None, :], int(num_points) + 1, axis=0)

    target_lengths = np.linspace(0.0, total_length, int(num_points) + 1)
    t_high_res = np.linspace(0.0, 1.0, int(high_res) + 1)
    t_uniform = np.interp(target_lengths, cum_arc_length, t_high_res)

    (r0, c0) = start
    (rm, cm) = mid
    (r1, c1) = goal
    a0 = r0
    a1 = 4 * rm - 3 * r0 - r1
    a2 = (r1 - r0) - a1
    b0 = c0
    b1 = 4 * cm - 3 * c0 - c1
    b2 = (c1 - c0) - b1
    rows = a0 + a1 * t_uniform + a2 * (t_uniform**2)
    cols = b0 + b1 * t_uniform + b2 * (t_uniform**2)
    return np.stack([rows, cols], axis=1)


def is_path_inside_grid(path: np.ndarray, grid_size: int) -> bool:
    p = np.asarray(path)
    return bool(np.all((p[:, 0] >= 0) & (p[:, 0] < grid_size) & (p[:, 1] >= 0) & (p[:, 1] < grid_size)))


def is_strictly_decreasing_rows(rows: np.ndarray) -> bool:
    return bool(np.all(np.diff(rows) < 0))


def sample_paths_polynomial(
    robot: tuple[int, int] = (239, 120),
    num_goals: int = 100,
    num_mid_points_per_goal: int = 30,
    num_samples: int = 100,
    grid_size: int = 240,
    goal: tuple[int, int] | None = None,
    include_random_goals: bool = True,
    random_seed: int | None = None,
) -> list[np.ndarray]:
    """Sample a fixed bank of quadratic paths in planner-grid row/col coordinates."""
    rng = random.Random(random_seed) if random_seed is not None else random
    goals: list[tuple[int, int]] = []
    if goal is not None:
        gr, gc = int(goal[0]), int(goal[1])
        if 0 <= gr < int(grid_size) and 0 <= gc < int(grid_size):
            goals.append((gr, gc))

    if include_random_goals:
        remaining = max(0, int(num_goals) - len(goals))
        goals.extend(sample_goals(remaining, grid_size=int(grid_size), rng=rng))

    if not goals:
        goals = sample_goals(num_goals, grid_size=int(grid_size), rng=rng)

    all_paths: list[np.ndarray] = []
    robot_rc = (int(robot[0]), int(robot[1]))
    for goal_rc in goals:
        for _ in range(int(num_mid_points_per_goal)):
            mid = sample_mid_for_goal(goal_rc, robot_rc, int(grid_size), rng=rng)
            path = uniformly_sample_by_arclength(
                robot_rc,
                mid,
                goal_rc,
                num_points=int(num_samples),
                high_res=1000,
            )
            if is_path_inside_grid(path, int(grid_size)) and is_strictly_decreasing_rows(path[:, 0]):
                all_paths.append(path.astype(np.float32))
    return all_paths
