from pathlib import Path
from typing import Optional
import yaml

from .models import SourceConfig
from .settings import settings


def load_sources(config_dir: Optional[Path] = None) -> list[SourceConfig]:
    """Load and validate all source YAML files from config_dir."""
    dir_ = config_dir or settings.config_dir
    sources: list[SourceConfig] = []
    for yaml_file in sorted(dir_.glob("sources_*.yaml")):
        sources.extend(_load_file(yaml_file))
    return sources


def _load_file(path: Path) -> list[SourceConfig]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    category = data["category"]
    defaults = data.get("defaults", {})

    result: list[SourceConfig] = []
    for raw in data.get("sources", []):
        if not raw.get("enabled", True):
            continue
        src = SourceConfig(
            name=raw["name"],
            home_url=raw["home_url"],
            category=category,
            tier=raw.get("tier", 3),
            language=raw.get("language", "en"),
            enabled=raw.get("enabled", True),
            tags=raw.get("tags", []),
            feed_url=raw.get("feed_url"),
            relevance_threshold=raw.get(
                "relevance_threshold", defaults.get("relevance_threshold", 5)
            ),
            max_articles_per_run=raw.get(
                "max_articles_per_run", defaults.get("max_articles_per_run", 30)
            ),
            rate_limit_rps=raw.get(
                "rate_limit_rps", defaults.get("rate_limit_rps", 0.5)
            ),
            fetch_timeout_s=raw.get(
                "fetch_timeout_s", defaults.get("fetch_timeout_s", 15)
            ),
            scrape_config=raw.get("scrape_config"),
            notes=raw.get("notes", ""),
        )
        result.append(src)
    return result
