from __future__ import annotations

import math
from typing import Iterable

import numpy as np

try:
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score
except Exception:  # pragma: no cover - optional dependency failure handled at runtime.
    KMeans = None
    silhouette_score = None

_SKLEARN_THREADPOOL_BYPASS_READY = False


def has_majority_of_high_cost_points(
    raw_topdown_score: np.ndarray,
    candidate_path: np.ndarray,
    num_points: int,
    footprint_px: int = 20,
    threshold_points_ratio: float = 0.5,
    threshold_cost: float = 0.8,
) -> bool:
    r_half = max(1, int(footprint_px)) // 2
    h, w = raw_topdown_score.shape[:2]

    for row, col in candidate_path[: int(num_points)]:
        r_coord = int(round(float(row)))
        c_coord = int(round(float(col)))
        r1 = max(0, r_coord - r_half)
        r2 = min(h, r_coord + r_half + 1)
        c1 = max(0, c_coord - r_half)
        c2 = min(w, c_coord + r_half + 1)
        region = raw_topdown_score[r1:r2, c1:c2]
        if region.size == 0:
            return True
        threshold_points = int(np.ceil(float(threshold_points_ratio) * region.size))
        if int(np.sum(region >= float(threshold_cost))) >= threshold_points:
            return True
    return False


def filter_paths_with_high_costs(
    candidate_paths: Iterable[np.ndarray],
    raw_topdown_score: np.ndarray,
    num_points: int,
    footprint_px: int = 20,
    threshold_points_ratio: float = 0.05,
    threshold_cost: float = 0.8,
) -> list[np.ndarray]:
    result: list[np.ndarray] = []
    for path in candidate_paths:
        if not has_majority_of_high_cost_points(
            raw_topdown_score,
            path,
            num_points=num_points,
            footprint_px=footprint_px,
            threshold_points_ratio=threshold_points_ratio,
            threshold_cost=threshold_cost,
        ):
            result.append(path)
    return result


def _ensure_sklearn_threadpool_bypass() -> None:
    """Install a no-op sklearn threadpool controller to avoid threadpoolctl stalls."""
    global _SKLEARN_THREADPOOL_BYPASS_READY
    if _SKLEARN_THREADPOOL_BYPASS_READY:
        return
    try:
        import sklearn.cluster._kmeans as sklearn_kmeans_module
        import sklearn.utils.parallel as sklearn_parallel
    except Exception:
        return

    class _NoopLimit:
        def __enter__(self) -> "_NoopLimit":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

    class _NoopThreadpoolController:
        def limit(self, limits: object = None, user_api: object = None) -> _NoopLimit:
            del limits, user_api
            return _NoopLimit()

        def info(self) -> list[object]:
            return []

    controller = _NoopThreadpoolController()
    sklearn_parallel._threadpool_controller = controller
    sklearn_parallel._get_threadpool_controller = lambda: controller
    sklearn_kmeans_module._get_threadpool_controller = lambda: controller
    _SKLEARN_THREADPOOL_BYPASS_READY = True


def adaptive_kmeans(
    paths: list[np.ndarray],
    min_clusters: int = 1,
    max_clusters: int = 10,
    random_state: int = 42,
    inertia_weight: float = 0.0001,
) -> tuple[np.ndarray, np.ndarray, int]:
    if KMeans is None or silhouette_score is None:
        raise RuntimeError("scikit-learn is required when planner clustering is enabled")
    if len(paths) == 0:
        raise ValueError("paths is empty")
    _ensure_sklearn_threadpool_bypass()

    feature_vectors = np.array([np.asarray(path, dtype=np.float32).flatten() for path in paths])
    best_score = -np.inf
    best_k = int(min_clusters)
    best_labels: np.ndarray | None = None
    best_centroids: np.ndarray | None = None

    max_clusters = min(int(max_clusters), len(paths))
    for k in range(int(min_clusters), max_clusters + 1):
        kmeans = KMeans(n_clusters=k, random_state=int(random_state), n_init=10)
        labels = kmeans.fit_predict(feature_vectors)
        if k == 1:
            score = -float(kmeans.inertia_) * float(inertia_weight)
        else:
            try:
                score = float(silhouette_score(feature_vectors, labels))
            except ValueError:
                score = -np.inf
        if score > best_score:
            best_score = score
            best_k = k
            best_labels = labels
            best_centroids = kmeans.cluster_centers_

    assert best_labels is not None and best_centroids is not None
    return best_labels, best_centroids, best_k


def merge_centroids_by_angle(
    centroids: np.ndarray,
    angle_threshold_deg: float,
) -> tuple[np.ndarray, list[list[int]], list[float]]:
    merged_centroids: list[np.ndarray] = []
    groups: list[list[int]] = []
    centroid_angles: list[float] = []

    for centroid in centroids:
        path = centroid.reshape(-1, 2)
        start_point = path[0]
        end_point = path[min(30, len(path) - 1)]
        angle_rad = math.atan2(end_point[0] - start_point[0], end_point[1] - start_point[1])
        centroid_angles.append(math.degrees(angle_rad))

    for idx, angle in enumerate(centroid_angles):
        best_group: list[int] | None = None
        best_diff = float("inf")
        for group in groups:
            min_diff = min(abs(angle - centroid_angles[i]) for i in group)
            if min_diff < best_diff:
                best_diff = min_diff
                best_group = group
        if best_group is not None and best_diff < float(angle_threshold_deg):
            best_group.append(idx)
        else:
            groups.append([idx])

    for group in groups:
        merged_centroids.append(np.mean(np.array([centroids[i] for i in group]), axis=0))
    return np.array(merged_centroids), groups, centroid_angles


def _angle_diff_deg(a_deg: float, b_deg: float) -> float:
    diff = abs(float(a_deg) - float(b_deg))
    return float(360.0 - diff if diff > 180.0 else diff)


def _path_orientation_deg(path_rc: np.ndarray, lookahead_index: int = 30) -> float:
    path = np.asarray(path_rc, dtype=np.float32).reshape(-1, 2)
    if path.shape[0] == 0:
        return 0.0
    start = path[0]
    end = path[min(max(1, int(lookahead_index)), path.shape[0] - 1)]
    return float(math.degrees(math.atan2(float(end[0] - start[0]), float(end[1] - start[1]))))


def select_best_group_by_closest_path_angle(
    paths: list[np.ndarray],
    labels: np.ndarray,
    groups: list[list[int]],
    robot_goal_angle: float,
    lookahead_index: int = 30,
) -> tuple[int, float]:
    """Select merged group using the closest-to-goal single path in each group."""
    best_diff_by_label: dict[int, float] = {}
    for path, lab in zip(paths, labels):
        lab_i = int(lab)
        diff = _angle_diff_deg(_path_orientation_deg(path, lookahead_index), robot_goal_angle)
        prev = best_diff_by_label.get(lab_i)
        if prev is None or diff < prev:
            best_diff_by_label[lab_i] = diff

    best_group_idx = 0
    best_group_diff = float("inf")
    for gi, group in enumerate(groups):
        group_best = min((best_diff_by_label.get(int(lab), float("inf")) for lab in group), default=float("inf"))
        if group_best < best_group_diff:
            best_group_idx = int(gi)
            best_group_diff = float(group_best)
    return best_group_idx, best_group_diff
