"""Cost calculation engine for OpenRouter usage."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from src.costs.models import CostBreakdown, CostEvent, ModelPricing, ProjectCost, UsageRecord

logger = logging.getLogger(__name__)


@dataclass
class CostEngineConfig:
    """Configuration for the CostEngine."""

    pricing_file: Path = Path("config/pricing.yaml")
    event_queue: Optional[asyncio.Queue] = None


class CostEngine:
    """
    Cost calculation engine for OpenRouter usage.

    Provides:
    - calculate(): Calculate cost for a single usage record
    - attribute(): Aggregate costs by project
    - get_pricing(): Look up pricing for a model

    Emits 'costs.calculated' events via asyncio.Queue when costs are calculated.
    """

    # Hardcoded fallback defaults for known providers (per 1K tokens)
    FALLBACK_DEFAULTS: Dict[str, Dict[str, Decimal]] = {
        "anthropic/": {"prompt": Decimal("0.003"), "completion": Decimal("0.015")},
        "openai/": {"prompt": Decimal("0.005"), "completion": Decimal("0.015")},
        "google/": {"prompt": Decimal("0.001"), "completion": Decimal("0.004")},
        "meta-llama/": {"prompt": Decimal("0.001"), "completion": Decimal("0.001")},
        "mistralai/": {"prompt": Decimal("0.001"), "completion": Decimal("0.003")},
        "cohere/": {"prompt": Decimal("0.001"), "completion": Decimal("0.005")},
        "qwen/": {"prompt": Decimal("0.0005"), "completion": Decimal("0.0005")},
        "deepseek/": {"prompt": Decimal("0.0002"), "completion": Decimal("0.0004")},
        "nousresearch/": {"prompt": Decimal("0.001"), "completion": Decimal("0.001")},
        "default": {"prompt": Decimal("0.001"), "completion": Decimal("0.003")},
    }

    def __init__(self, config: Optional[CostEngineConfig] = None):
        self.config = config or CostEngineConfig()
        self._pricing_cache: Dict[str, ModelPricing] = {}
        self._pricing_loaded = False
        self._fallback_loaded = False

    def _load_pricing(self) -> None:
        """Load pricing from YAML config file."""
        if self._pricing_loaded:
            return

        pricing_path = self.config.pricing_file
        if not pricing_path.is_absolute():
            # Resolve relative to project root (parent of src/)
            project_root = Path(__file__).parent.parent.parent
            pricing_path = project_root / pricing_path

        try:
            with open(pricing_path, "r") as f:
                data = yaml.safe_load(f)

            # Load model pricing
            models = data.get("models", {})
            for model_id, pricing_data in models.items():
                self._pricing_cache[model_id] = ModelPricing(
                    model=model_id,
                    prompt_price_per_1k=Decimal(str(pricing_data["prompt"])),
                    completion_price_per_1k=Decimal(str(pricing_data["completion"])),
                    cached_prompt_price_per_1k=(
                        Decimal(str(pricing_data["cached_prompt"]))
                        if "cached_prompt" in pricing_data
                        else None
                    ),
                    updated_at=(
                        datetime.fromisoformat(pricing_data["updated"].replace("Z", "+00:00"))
                        if "updated" in pricing_data
                        else None
                    ),
                )

            # Load fallback defaults from config (overrides hardcoded)
            fallbacks = data.get("fallback_defaults", {})
            for prefix, pricing in fallbacks.items():
                if prefix in self.FALLBACK_DEFAULTS or prefix == "default":
                    self.FALLBACK_DEFAULTS[prefix] = {
                        "prompt": Decimal(str(pricing["prompt"])),
                        "completion": Decimal(str(pricing["completion"])),
                    }

            self._pricing_loaded = True
            logger.info(f"Loaded pricing for {len(self._pricing_cache)} models from {pricing_path}")

        except FileNotFoundError:
            logger.warning(f"Pricing file not found at {pricing_path}, using hardcoded defaults only")
            self._pricing_loaded = True
        except Exception as e:
            logger.error(f"Failed to load pricing: {e}, using hardcoded defaults only")
            self._pricing_loaded = True

    def get_pricing(self, model: str) -> ModelPricing:
        """
        Get pricing for a model.

        Looks up in order:
        1. Exact match in pricing.yaml
        2. Provider prefix fallback from pricing.yaml
        3. Hardcoded provider prefix defaults
        4. Global default
        """
        self._load_pricing()

        # 1. Exact match
        if model in self._pricing_cache:
            return self._pricing_cache[model]

        # 2. Provider prefix fallback from config
        for prefix, fallback in self.FALLBACK_DEFAULTS.items():
            if prefix != "default" and model.startswith(prefix):
                return ModelPricing(
                    model=model,
                    prompt_price_per_1k=fallback["prompt"],
                    completion_price_per_1k=fallback["completion"],
                )

        # 3. Global default
        default = self.FALLBACK_DEFAULTS.get("default", {"prompt": Decimal("0.001"), "completion": Decimal("0.003")})
        return ModelPricing(
            model=model,
            prompt_price_per_1k=default["prompt"],
            completion_price_per_1k=default["completion"],
        )

    def calculate(self, usage: UsageRecord) -> CostBreakdown:
        """
        Calculate cost breakdown for a single usage record.

        Uses the model's pricing to compute prompt and completion costs
        based on token counts.
        """
        pricing = self.get_pricing(usage.model)

        prompt_cost = (Decimal(usage.prompt_tokens) / Decimal(1000)) * pricing.prompt_price_per_1k
        completion_cost = (Decimal(usage.completion_tokens) / Decimal(1000)) * pricing.completion_price_per_1k
        total_cost = (prompt_cost + completion_cost).quantize(Decimal("0.000001"))

        return CostBreakdown(
            model=usage.model,
            project=usage.project,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            prompt_cost=prompt_cost.quantize(Decimal("0.000001")),
            completion_cost=completion_cost.quantize(Decimal("0.000001")),
            total_cost=total_cost,
            pricing_used=pricing,
            timestamp=usage.timestamp,
            request_id=usage.request_id,
        )

    def attribute(self, usage: List[UsageRecord]) -> Dict[str, ProjectCost]:
        """
        Attribute costs to projects.

        Aggregates usage records by project, computing total costs,
        token counts, and per-model breakdowns.
        """
        if not usage:
            return {}

        # Group by project
        project_data: Dict[str, Dict] = {}

        for record in usage:
            breakdown = self.calculate(record)
            project = record.project

            if project not in project_data:
                project_data[project] = {
                    "total_cost": Decimal("0"),
                    "total_prompt_tokens": 0,
                    "total_completion_tokens": 0,
                    "total_tokens": 0,
                    "model_breakdown": {},
                    "record_count": 0,
                    "period_start": record.timestamp,
                    "period_end": record.timestamp,
                }

            data = project_data[project]
            data["total_cost"] += breakdown.total_cost
            data["total_prompt_tokens"] += record.prompt_tokens
            data["total_completion_tokens"] += record.completion_tokens
            data["total_tokens"] += record.total_tokens
            data["model_breakdown"][record.model] = (
                data["model_breakdown"].get(record.model, Decimal("0")) + breakdown.total_cost
            )
            data["record_count"] += 1
            data["period_start"] = min(data["period_start"], record.timestamp)
            data["period_end"] = max(data["period_end"], record.timestamp)

        # Convert to ProjectCost objects
        result = {}
        for project, data in project_data.items():
            result[project] = ProjectCost(
                project=project,
                total_cost=data["total_cost"].quantize(Decimal("0.000001")),
                total_prompt_tokens=data["total_prompt_tokens"],
                total_completion_tokens=data["total_completion_tokens"],
                total_tokens=data["total_tokens"],
                model_breakdown={k: v.quantize(Decimal("0.000001")) for k, v in data["model_breakdown"].items()},
                record_count=data["record_count"],
                period_start=data["period_start"],
                period_end=data["period_end"],
            )

        # Emit costs.calculated event
        self._emit_cost_event(list(result.values()))

        return result

    def _emit_cost_event(self, project_costs: List[ProjectCost]) -> None:
        """Emit costs.calculated event via asyncio.Queue."""
        if self.config.event_queue is not None:
            event = CostEvent(
                event_type="costs.calculated",
                project_costs=project_costs,
                timestamp=datetime.utcnow(),
            )
            try:
                self.config.event_queue.put_nowait(event)
                logger.debug(f"Emitted costs.calculated event with {len(project_costs)} projects")
            except asyncio.QueueFull:
                logger.warning("Event queue full, dropping costs.calculated event")
        else:
            logger.debug("No event queue configured, skipping event emission")

    def reload_pricing(self) -> None:
        """Force reload of pricing from config file."""
        self._pricing_cache.clear()
        self._pricing_loaded = False
        self._load_pricing()