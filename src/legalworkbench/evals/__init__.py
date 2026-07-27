"""Benchmark evaluation package."""

from legalworkbench.evals.baseline import BaselineEvaluator, BaselineResultRow, format_baseline_table
from legalworkbench.evals.human_benchmark import HumanBenchmarkRunner, load_human_benchmark
from legalworkbench.evals.real_benchmark import (
    REAL_BENCHMARK_METHODS,
    RealBenchmarkEvaluator,
    RealBenchmarkMethodResult,
    RealBenchmarkReport,
    format_real_benchmark_table,
    load_real_benchmark,
)
from legalworkbench.evals.runner import BenchmarkRunner

__all__ = [
    "BaselineEvaluator",
    "BaselineResultRow",
    "BenchmarkRunner",
    "HumanBenchmarkRunner",
    "REAL_BENCHMARK_METHODS",
    "RealBenchmarkEvaluator",
    "RealBenchmarkMethodResult",
    "RealBenchmarkReport",
    "format_baseline_table",
    "format_real_benchmark_table",
    "load_human_benchmark",
    "load_real_benchmark",
]
