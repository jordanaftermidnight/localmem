"""LOCALMEM configuration."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator


_ENV_PATTERN = re.compile(
    r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}"
)


def _interpolate_env(node: Any) -> Any:
    """Recursively replace ${VAR} or ${VAR:-default} in string values with
    environment variables. Pulled out so secret-bearing fields (api_key,
    qdrant_api_key) don't have to live plaintext in localmem.yaml."""
    if isinstance(node, str):
        def _sub(match: re.Match) -> str:
            name = match.group("name")
            default = match.group("default")
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            # Leave the literal ${VAR} in place so a later reader can spot it.
            return match.group(0)
        return _ENV_PATTERN.sub(_sub, node)
    if isinstance(node, dict):
        return {k: _interpolate_env(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_interpolate_env(v) for v in node]
    return node


class ServerConfig(BaseModel):
    host: str = "localhost"
    port: int = 8781
    transport: str = "sse"


class StorageConfig(BaseModel):
    base_path: str = "./data"
    qdrant_path: str = "./data/qdrant"
    qdrant_mode: str = "local"  # "local" (path-backed) or "server" (URL-backed)
    qdrant_url: str | None = None
    qdrant_api_key: str | None = None
    sqlite_path: str = "./data/localmem.db"
    graph_path: str = "./data/graph.json"

    @model_validator(mode="after")
    def _validate_qdrant_mode(self) -> "StorageConfig":
        if self.qdrant_mode not in ("local", "server"):
            raise ValueError(
                f"qdrant_mode must be 'local' or 'server', got {self.qdrant_mode!r}"
            )
        if self.qdrant_mode == "server" and not self.qdrant_url:
            raise ValueError(
                "qdrant_mode='server' requires qdrant_url (e.g. http://qdrant:6333)"
            )
        return self


class EmbeddingConfig(BaseModel):
    model: str = "all-MiniLM-L6-v2"
    sparse_model: str = "Qdrant/bm25"
    device: str = "auto"  # "auto", "cpu", "cuda", or "mps"


class LoadingConfig(BaseModel):
    l1_top_k: int = 15
    l1_max_tokens: int = 120
    decay_rate_per_day: float = 0.01


class GraphConfig(BaseModel):
    persistence_debounce_seconds: float = 5.0
    max_community_size: int = 50


class ConcurrencyConfig(BaseModel):
    write_timeout_seconds: float = 30.0


class LoggingConfig(BaseModel):
    level: str = "INFO"
    format: str = "text"  # "text" or "json"
    file: str | None = None
    max_bytes: int = 10_000_000  # 10MB
    backup_count: int = 3


class ToolSequenceDetectorConfig(BaseModel):
    """Detect frequent tool→tool transitions in a wing/room of JSON entries
    shaped like {"tool": "...", "success": true}. Disabled unless wing+room set."""
    wing: str | None = None
    room: str | None = None


class ProviderPreferenceDetectorConfig(BaseModel):
    """Detect dominant/split/rotating preference patterns from JSON entries
    shaped like {"task_type": "...", "provider": "..."}. Disabled unless set."""
    wing: str | None = None
    room: str | None = None


class NodeClusterDetectorConfig(BaseModel):
    """Detect Louvain communities among graph nodes with a shared type/prefix.
    Disabled unless either node_type or node_prefix is set."""
    node_type: str | None = None
    node_prefix: str | None = None


class CrossWingCorrelationDetectorConfig(BaseModel):
    """Detect temporal co-occurrence patterns across the listed wings.
    Disabled unless wings has 2+ entries."""
    wings: list[str] = Field(default_factory=list)


class IntelligenceDetectorsConfig(BaseModel):
    tool_sequences: ToolSequenceDetectorConfig = Field(
        default_factory=ToolSequenceDetectorConfig
    )
    provider_preferences: ProviderPreferenceDetectorConfig = Field(
        default_factory=ProviderPreferenceDetectorConfig
    )
    node_clusters: NodeClusterDetectorConfig = Field(
        default_factory=NodeClusterDetectorConfig
    )
    cross_wing_correlations: CrossWingCorrelationDetectorConfig = Field(
        default_factory=CrossWingCorrelationDetectorConfig
    )


class IntelligenceConfig(BaseModel):
    pattern_min_frequency: int = 3
    correlation_window_hours: int = 24
    correlation_min_strength: float = 0.5
    alert_room: str = "intelligence-alerts"
    detectors: IntelligenceDetectorsConfig = Field(
        default_factory=IntelligenceDetectorsConfig
    )


class DashboardConfig(BaseModel):
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8782
    auth_enabled: bool = False
    api_key: str | None = None
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    ws_push_interval_seconds: float = 2.0


class WingRetentionPolicy(BaseModel):
    """Per-wing override for retention thresholds. Any field left None falls back
    to RetentionConfig.default. max_age_days=None means: never archive (cold tier
    skipped). soft_age_days=None means: never consolidate."""
    soft_age_days: int | None = None
    max_age_days: int | None = None
    importance_floor: float | None = None


class RetentionDefaults(BaseModel):
    soft_age_days: int = 30
    max_age_days: int | None = 365
    importance_floor: float = 0.1


class ConsolidationConfig(BaseModel):
    """How groups are summarized when entries cross the soft gate."""
    group_by: list[str] = Field(default_factory=lambda: ["wing", "room", "week"])
    summarizer: str = "template"  # "template" | "llm" (llm reserved for v0.4.0)
    llm_model: str | None = None
    top_n_per_summary: int = 5
    min_group_size: int = 3  # don't consolidate trivially small groups

    cron_enabled: bool = True
    cron_schedule: str = "0 4 * * *"  # daily 04:00 local

    backpressure_enabled: bool = True
    trigger_count_per_wing: int = 10000
    min_interval_seconds: int = 300  # cooldown between consolidations of same wing


class ArchiveConfig(BaseModel):
    enabled: bool = True
    path: str = "./data/archive"
    format: str = "jsonl.zst"
    compression_level: int = 7
    snapshot_before_transition: bool = True


class RetentionConfig(BaseModel):
    enabled: bool = False  # off by default — opt-in
    default: RetentionDefaults = Field(default_factory=RetentionDefaults)
    wings: dict[str, WingRetentionPolicy] = Field(default_factory=dict)
    consolidation: ConsolidationConfig = Field(default_factory=ConsolidationConfig)
    archive: ArchiveConfig = Field(default_factory=ArchiveConfig)

    @model_validator(mode="after")
    def _validate_wings(self) -> "RetentionConfig":
        # Allow explicit null max_age_days only on shared. Unset (= use default)
        # is fine on any wing — only flag the case where the user wrote null.
        for name, policy in self.wings.items():
            if (
                "max_age_days" in policy.model_fields_set
                and policy.max_age_days is None
                and name != "shared"
            ):
                raise ValueError(
                    f"retention.wings.{name}.max_age_days=null only allowed for 'shared'"
                )
        return self

    def policy_for(self, wing: str) -> dict:
        """Return effective policy for a wing: explicit per-wing override over
        default. Distinguishes 'field omitted' (use default) from 'field set to
        null' (use null — only valid for max_age_days on shared)."""
        wing_policy = self.wings.get(wing)

        def _resolve(field: str, default_value):
            if wing_policy is None:
                return default_value
            if field in wing_policy.model_fields_set:
                return getattr(wing_policy, field)
            return default_value

        return {
            "soft_age_days": _resolve("soft_age_days", self.default.soft_age_days),
            "max_age_days": _resolve("max_age_days", self.default.max_age_days),
            "importance_floor": _resolve(
                "importance_floor", self.default.importance_floor
            ),
        }


SHARED_WING = "shared"


class LocalmemConfig(BaseModel):
    # Agent-owned wing namespaces. Each value is an arbitrary string used as a
    # storage partition for one agent's memory. The reserved "shared" wing is
    # always implicitly available for cross-agent context and never needs to
    # appear here.
    wings: list[str] = Field(default_factory=lambda: ["default"])

    server: ServerConfig = Field(default_factory=ServerConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    loading: LoadingConfig = Field(default_factory=LoadingConfig)
    graph: GraphConfig = Field(default_factory=GraphConfig)
    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    intelligence: IntelligenceConfig = Field(default_factory=IntelligenceConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)

    @model_validator(mode="after")
    def _validate_wings(self) -> "LocalmemConfig":
        if SHARED_WING in self.wings:
            raise ValueError(
                f"'{SHARED_WING}' is reserved and implicit — remove it from wings"
            )
        seen: set[str] = set()
        for w in self.wings:
            if not w or not isinstance(w, str):
                raise ValueError(f"wing names must be non-empty strings, got {w!r}")
            if w in seen:
                raise ValueError(f"duplicate wing name: {w!r}")
            seen.add(w)
        return self

    def all_wings(self) -> list[str]:
        """Agent wings plus the reserved 'shared' namespace, in declaration order."""
        return [*self.wings, SHARED_WING]


def load_config(path: str | Path = "localmem.yaml") -> LocalmemConfig:
    p = Path(path)
    if p.exists():
        with open(p) as f:
            raw = yaml.safe_load(f) or {}
        raw = _interpolate_env(raw)
        return LocalmemConfig(**raw)
    return LocalmemConfig()
