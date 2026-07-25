from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image


def load_config(path: str | Path) -> tuple[dict[str, Any], Path]:
    cfg_path = Path(path).expanduser().resolve()
    if not cfg_path.exists():
        raise FileNotFoundError(f"Config file not found: {cfg_path}")
    suffix = cfg_path.suffix.lower()
    if suffix == ".json":
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    elif suffix in {".yaml", ".yml"}:
        try:
            import yaml
        except ModuleNotFoundError as exc:
            raise ModuleNotFoundError("PyYAML is required to load YAML configs. Install pyyaml.") from exc
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    else:
        raise ValueError(f"Unsupported config suffix {suffix!r}; use .yaml, .yml, or .json")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"Config root must be a mapping, got {type(data).__name__}")
    return data, cfg_path.parent


def deep_get(data: dict[str, Any], path: str, default: Any = None) -> Any:
    cur: Any = data
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def resolve_path(value: str | Path | None, base_dir: Path, repo_root: Path | None = None) -> Path | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    candidate = (base_dir / path).resolve()
    if candidate.exists() or repo_root is None:
        return candidate
    return (repo_root / path).resolve()


def load_matrix(path_or_value: Any, shape: tuple[int, int], base_dir: Path, repo_root: Path | None, name: str) -> np.ndarray:
    if isinstance(path_or_value, (str, Path)):
        path = resolve_path(path_or_value, base_dir=base_dir, repo_root=repo_root)
        if path is None or not path.exists():
            raise FileNotFoundError(f"{name} file not found: {path}")
        suffix = path.suffix.lower()
        if suffix == ".npy":
            arr = np.load(path)
        elif suffix == ".json":
            arr = np.asarray(json.loads(path.read_text(encoding="utf-8")), dtype=np.float64)
        elif suffix in {".yaml", ".yml"}:
            try:
                import yaml
            except ModuleNotFoundError as exc:
                raise ModuleNotFoundError("PyYAML is required to load YAML matrix files. Install pyyaml.") from exc
            arr = np.asarray(yaml.safe_load(path.read_text(encoding="utf-8")), dtype=np.float64)
        else:
            raise ValueError(f"Unsupported {name} file suffix {suffix!r}: {path}")
    else:
        arr = np.asarray(path_or_value, dtype=np.float64)
    if arr.shape == shape:
        return arr.astype(np.float64)
    if arr.size == shape[0] * shape[1]:
        return arr.reshape(shape).astype(np.float64)
    raise ValueError(f"{name} must have shape {shape}, got {arr.shape}")


def load_rgb_image(path: str | Path) -> np.ndarray:
    img = Image.open(path).convert("RGB")
    return np.asarray(img, dtype=np.uint8)


def load_depth_m(path: str | Path, unit: str = "m") -> np.ndarray:
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"Depth file not found: {p}")
    suffix = p.suffix.lower()
    if suffix == ".npy":
        depth = np.asarray(np.load(p))
    else:
        depth = np.asarray(Image.open(p))
        if depth.ndim == 3:
            depth = depth[..., 0]

    depth = depth.astype(np.float32)
    unit_l = str(unit).lower()
    if unit_l in {"m", "meter", "meters"}:
        return depth
    if unit_l in {"mm", "millimeter", "millimeters"}:
        return depth / 1000.0
    raise ValueError(f"Unsupported depth unit {unit!r}; use 'm' or 'mm'")


def save_json(path: str | Path, payload: dict[str, Any]) -> None:
    Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def to_builtin(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {str(k): to_builtin(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_builtin(v) for v in value]
    if isinstance(value, tuple):
        return [to_builtin(v) for v in value]
    return value
