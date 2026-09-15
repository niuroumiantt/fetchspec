"""Declarative, bounded official-site spec crawler."""

from .catalog import load_rule, load_rules, ROOT
from .crawl import crawl

__all__ = ["load_rule", "load_rules", "crawl", "ROOT"]
