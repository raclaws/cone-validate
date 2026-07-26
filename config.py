#!/usr/bin/env python3
"""
Configuration management for cone-validate.

Loads settings from (in priority order):
  1. Environment variables (highest priority, 12-factor style)
  2. Config file specified via --config CLI flag
  3. ~/.cone-validate/config.yaml
  4. Built-in defaults (lowest priority)

Environment variables:
  CONE_TARGET_DIR      - Directory to analyze (e.g., /path/to/project/src)
  CONE_PROJECT_ROOT    - Project root for tsc (e.g., /path/to/project)
  CONE_GATEWAY_URL     - LLM gateway endpoint URL
  CONE_MODEL_CHEAP     - Fast/cheap model identifier
  CONE_MODEL_STRONG    - Powerful model for escalation
  CONE_LOG_DIR         - Directory for debug logs
  CONE_MAX_RETRIES     - Max retry attempts before escalation
  GATEWAY_API_KEY      - API key for gateway authentication
"""

import os
from pathlib import Path
from typing import Any

import yaml

# ── Defaults ──────────────────────────────────────────────────────────────────
_DEFAULTS = {
    "target_dir": "",
    "project_root": "",
    "gateway_url": "",
    "model_cheap": "claude-haiku-4.5",
    "model_strong": "kr/claude-sonnet-4.6",
    "log_dir": "",
    "max_retries": 3,
}

# ── Config paths ──────────────────────────────────────────────────────────────
DEFAULT_CONFIG_PATH = Path.home() / ".cone-validate" / "config.yaml"
_MODULE_DIR = Path(__file__).parent

# Global config instance (lazy loaded)
_config: dict[str, Any] | None = None
_config_path: Path | None = None


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load YAML config file, return empty dict if not found."""
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _env_override(config: dict[str, Any]) -> dict[str, Any]:
    """Apply environment variable overrides (12-factor style)."""
    env_map = {
        "target_dir": "CONE_TARGET_DIR",
        "project_root": "CONE_PROJECT_ROOT",
        "gateway_url": "CONE_GATEWAY_URL",
        "model_cheap": "CONE_MODEL_CHEAP",
        "model_strong": "CONE_MODEL_STRONG",
        "log_dir": "CONE_LOG_DIR",
        "max_retries": "CONE_MAX_RETRIES",
    }
    
    result = config.copy()
    for key, env_var in env_map.items():
        if env_var in os.environ:
            value = os.environ[env_var]
            # Convert to int for max_retries
            if key == "max_retries":
                try:
                    value = int(value)
                except ValueError:
                    pass
            result[key] = value
    
    return result


def load_config(config_path: Path | str | None = None) -> dict[str, Any]:
    """
    Load configuration from file and environment.
    
    Args:
        config_path: Optional path to config file. If None, uses default.
    
    Returns:
        Configuration dictionary with all settings.
    """
    global _config, _config_path
    
    # Determine config file path
    if config_path is not None:
        path = Path(config_path)
    else:
        path = DEFAULT_CONFIG_PATH
    
    _config_path = path
    
    # Start with defaults
    config = _DEFAULTS.copy()
    
    # Layer file config
    file_config = _load_yaml(path)
    for key in config:
        if key in file_config:
            config[key] = file_config[key]
    
    # Apply env overrides
    config = _env_override(config)
    
    # Set default log_dir if not specified
    if not config["log_dir"]:
        config["log_dir"] = str(_MODULE_DIR / "debug_logs")
    
    _config = config
    return config


def get_config() -> dict[str, Any]:
    """Get current config, loading if necessary."""
    global _config
    if _config is None:
        load_config()
    return _config


def reload_config(config_path: Path | str | None = None) -> dict[str, Any]:
    """Force reload configuration."""
    global _config
    _config = None
    return load_config(config_path)


# ── Convenience accessors ─────────────────────────────────────────────────────
# These provide typed access and Path conversion

def get_target_dir() -> Path:
    """Get TARGET_DIR as Path. Raises if not configured."""
    val = get_config()["target_dir"]
    if not val:
        raise ValueError("target_dir not configured. Set CONE_TARGET_DIR or config file.")
    return Path(val)


def get_project_root() -> Path:
    """Get PROJECT_ROOT as Path. Raises if not configured."""
    val = get_config()["project_root"]
    if not val:
        raise ValueError("project_root not configured. Set CONE_PROJECT_ROOT or config file.")
    return Path(val)


def get_gateway_url() -> str:
    """Get GATEWAY_URL. Raises if not configured."""
    val = get_config()["gateway_url"]
    if not val:
        raise ValueError("gateway_url not configured. Set CONE_GATEWAY_URL or config file.")
    return val


def get_api_key() -> str:
    """Get API key from GATEWAY_API_KEY env var."""
    return os.environ.get("GATEWAY_API_KEY", "")


def get_model_cheap() -> str:
    """Get cheap/fast model identifier."""
    return get_config()["model_cheap"]


def get_model_strong() -> str:
    """Get strong/escalation model identifier."""
    return get_config()["model_strong"]


def get_log_dir() -> Path:
    """Get LOG_DIR as Path, creating if necessary."""
    path = Path(get_config()["log_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_max_retries() -> int:
    """Get max retry count."""
    return int(get_config()["max_retries"])


# ── Exported constants (for backward compatibility) ───────────────────────────
# These are loaded lazily when first accessed

class _LazyConfig:
    """Lazy config loader for module-level constants."""
    
    @property
    def TARGET_DIR(self) -> Path:
        return get_target_dir()
    
    @property
    def PROJECT_ROOT(self) -> Path:
        return get_project_root()
    
    @property
    def GATEWAY_URL(self) -> str:
        return get_gateway_url()
    
    @property
    def API_KEY(self) -> str:
        return get_api_key()
    
    @property
    def MODEL_CHEAP(self) -> str:
        return get_model_cheap()
    
    @property
    def MODEL_STRONG(self) -> str:
        return get_model_strong()
    
    @property
    def LOG_DIR(self) -> Path:
        return get_log_dir()
    
    @property
    def MAX_RETRIES(self) -> int:
        return get_max_retries()


# Create lazy config instance
_lazy = _LazyConfig()

# Export as module-level for backward compatibility
# Usage: from config import TARGET_DIR, MODEL_CHEAP, etc.
def __getattr__(name: str):
    """Module-level attribute access for lazy config values."""
    if hasattr(_lazy, name):
        return getattr(_lazy, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    # Debug: print current config
    import json
    config = load_config()
    print("Current configuration:")
    print(json.dumps(config, indent=2, default=str))
    print(f"\nConfig file: {_config_path}")
    print(f"API key set: {'yes' if get_api_key() else 'no'}")
