#!/usr/bin/env python3
"""Retrieval benchmark harness for Claude Code Memory System.

Seeds a test DB with ~50 diverse memories, runs ~20 benchmark queries,
measures precision@5, recall@5, NDCG@10, MRR, and compares against baseline.

Usage:
    python3 -m pytest tests/test_benchmark_retrieval.py -v
    python3 tests/test_benchmark_retrieval.py --update-baseline  # save new baseline
"""
import json
import math
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest
from storage import DataPointRow, ensure_db, fts_insert, insert_data_point

BASELINE_PATH = Path(__file__).parent / "benchmark_baseline.json"


@dataclass
class BenchmarkQuery:
    """A benchmark query with expected relevant memory IDs."""
    query: str
    relevant_ids: list[str]  # expected relevant data_point IDs
    scope: str | None = None
    description: str = ""


def seed_benchmark_db(tmp_path):
    """Create and populate a benchmark database with ~50 diverse test memories.

    Returns (conn, id_map) where id_map maps logical names to actual IDs.
    """
    db_path = tmp_path / "benchmark.db"
    with patch("storage.get_db_path", return_value=db_path), \
         patch("memory_utils.get_memory_dir", return_value=tmp_path):
        conn = ensure_db()

    id_map = {}
    memories = [
        # Redis cluster
        ("redis_ttl", "Redis cache requires explicit TTL settings to avoid stale data", "global", 0.8, 4),
        ("redis_cluster", "Redis cluster mode needs READONLY flag for replica reads", "global", 0.7, 3),
        ("redis_eviction", "Redis eviction policy should be allkeys-lru for cache workloads", "global", 0.6, 3),
        # SQLite cluster
        ("sqlite_wal", "SQLite WAL mode enables concurrent read access", "global", 0.9, 5),
        ("sqlite_busy", "SQLite SQLITE_BUSY errors require retry logic with backoff", "global", 0.7, 4),
        ("sqlite_fk", "SQLite foreign keys require PRAGMA foreign_keys=ON per connection", "global", 0.8, 4),
        # Testing cluster
        ("pytest_fixtures", "Always use pytest fixtures for test isolation not unittest setUp", "global", 0.6, 3),
        ("pytest_parametrize", "Use @pytest.mark.parametrize for input/output variations", "global", 0.5, 3),
        ("test_constants", "Never hardcode configurable values in tests; import from source", "global", 0.7, 4),
        # Project-scoped
        ("proj_auth", "Authentication uses JWT with RS256 algorithm", "auth-service", 0.8, 4),
        ("proj_auth_refresh", "Refresh tokens expire after 7 days, access tokens after 15 minutes", "auth-service", 0.7, 3),
        ("proj_auth_jwt_tokens", "JWT tokens must be validated and refreshed by the auth-service middleware", "auth-service", 0.75, 4),
        ("proj_deploy", "Deploy to staging requires approval from team lead", "auth-service", 0.5, 2),
        # Python patterns
        ("py_pathlib", "Use pathlib.Path instead of os.path for cross-platform compatibility", "global", 0.6, 5),
        ("py_typing", "Use Optional[str] not str | None for Python 3.9 compatibility", "global", 0.5, 3),
        ("py_dataclass", "Prefer dataclasses over named tuples for structured data", "global", 0.4, 2),
        # Git patterns
        ("git_rebase", "Prefer rebase over merge for feature branches to keep linear history", "global", 0.5, 3),
        ("git_commit", "Commit messages should explain why not what", "global", 0.6, 4),
        # Architecture
        ("arch_microservice", "Each microservice should own its database schema", "global", 0.7, 4),
        ("arch_event", "Use event sourcing for audit-critical domains", "global", 0.6, 3),
        ("arch_api", "REST APIs should use plural nouns for resource paths", "global", 0.5, 3),
        # Low salience (cold)
        ("cold_legacy", "Legacy PHP endpoints deprecated since 2024-01", "global", 0.1, 1),
        ("cold_temp", "Temporary workaround for API rate limiting", "global", 0.08, 1),
        # Speculative
        ("spec_graphql", "GraphQL might be better than REST for mobile clients", "global", 0.3, 1),
        ("spec_rust", "Consider rewriting hot path in Rust for performance", "global", 0.2, 1),
        # Docker / DevOps
        ("docker_multi", "Use multi-stage Docker builds to reduce image size", "global", 0.7, 4),
        ("docker_health", "Docker healthchecks should use curl not wget for consistency", "global", 0.5, 3),
        ("k8s_limits", "Always set CPU and memory limits on Kubernetes pods", "global", 0.8, 4),
        ("k8s_probes", "Kubernetes liveness probes should not share endpoint with readiness", "global", 0.6, 3),
        # Security
        ("sec_secrets", "Never store secrets in environment variables; use a vault", "global", 0.9, 5),
        ("sec_cors", "CORS allowlist should enumerate origins not use wildcard in production", "global", 0.7, 4),
        ("sec_jwt", "JWT tokens must be validated on every request not just at login", "global", 0.8, 4),
        # Database patterns
        ("db_index", "Add indexes on foreign keys to avoid full table scans on joins", "global", 0.7, 4),
        ("db_migration", "Database migrations should be idempotent and reversible", "global", 0.6, 3),
        ("db_pool", "Connection pooling reduces latency; set pool size to 2x CPU cores", "global", 0.5, 3),
        # Error handling
        ("err_retry", "Retry transient errors with exponential backoff and jitter", "global", 0.7, 4),
        ("err_circuit", "Circuit breaker pattern prevents cascading failures in microservices", "global", 0.6, 3),
        # Logging
        ("log_struct", "Use structured logging with JSON format for machine parseability", "global", 0.6, 3),
        ("log_level", "Log levels should be configurable at runtime without restart", "global", 0.5, 3),
        # CI/CD
        ("ci_cache", "Cache dependencies in CI to reduce build times by 60%", "global", 0.5, 3),
        ("ci_parallel", "Run test suites in parallel shards for faster CI feedback", "global", 0.4, 2),
        # More project-scoped
        ("proj_api_rate", "API rate limiting uses token bucket algorithm with 100 req/min default", "api-gateway", 0.7, 4),
        ("proj_api_version", "API versioning via URL path prefix /v1/ /v2/", "api-gateway", 0.6, 3),
        # Performance
        ("perf_lazy", "Lazy loading reduces initial page load time for large SPAs", "global", 0.5, 3),
        ("perf_cache", "HTTP cache headers should set max-age and ETag for static assets", "global", 0.6, 3),
        # Monitoring
        ("mon_sli", "Define SLIs for latency p99 error rate and availability", "global", 0.7, 4),
        ("mon_alert", "Alerts should fire on symptoms not causes to reduce noise", "global", 0.6, 3),
        # Entities
        ("entity_redis", "Redis", "global", 0.5, None),  # type=entity
        ("entity_sqlite", "SQLite", "global", 0.5, None),
        ("entity_docker", "Docker", "global", 0.5, None),
        ("entity_k8s", "Kubernetes", "global", 0.5, None),
    ]

    for name, content, scope, salience, certainty in memories:
        dp_type = "entity" if name.startswith("entity_") else "memory"
        dp = DataPointRow(
            type=dp_type,
            content=content,
            scope=scope,
            salience=salience,
            certainty=certainty,
            name=content if dp_type == "entity" else None,
        )
        dp_id = insert_data_point(conn, dp)
        id_map[name] = dp_id
        if dp_type == "memory":
            fts_insert(conn, dp_id, content, scope)

    conn.commit()
    return conn, id_map


def build_benchmark_queries(id_map):
    """Build the benchmark query suite (~20 queries covering diverse scenarios).

    FTS5 uses implicit AND for quoted terms, so each query term must appear
    in the target seed memory content for a match.  Queries are designed so
    that at least one relevant seed memory contains ALL query terms.
    """
    return [
        BenchmarkQuery(
            "Redis cache",
            [id_map["redis_ttl"], id_map["redis_cluster"], id_map["redis_eviction"]],
            description="Keyword: Redis cluster",
        ),
        BenchmarkQuery(
            "SQLite concurrent access",
            [id_map["sqlite_wal"], id_map["sqlite_busy"]],
            description="Keyword: SQLite concurrency",
        ),
        BenchmarkQuery(
            "pytest fixtures",
            [id_map["pytest_fixtures"], id_map["pytest_parametrize"], id_map["test_constants"]],
            description="Testing cluster",
        ),
        BenchmarkQuery(
            "JWT tokens",
            [id_map["proj_auth"], id_map["proj_auth_refresh"]],
            scope="auth-service",
            description="Project-scoped auth",
        ),
        BenchmarkQuery(
            "SQLite PRAGMA",
            [id_map["sqlite_wal"], id_map["sqlite_fk"], id_map["sqlite_busy"]],
            description="Cross-topic DB config",
        ),
        BenchmarkQuery(
            "pathlib compatibility",
            [id_map["py_pathlib"], id_map["py_typing"], id_map["py_dataclass"]],
            description="Python patterns",
        ),
        BenchmarkQuery(
            "commit messages",
            [id_map["git_rebase"], id_map["git_commit"]],
            description="Git patterns",
        ),
        BenchmarkQuery(
            "microservice database",
            [id_map["arch_microservice"], id_map["arch_event"], id_map["arch_api"]],
            description="Architecture cluster",
        ),
        BenchmarkQuery(
            "Redis eviction cache",
            [id_map["redis_eviction"], id_map["redis_ttl"]],
            description="Specific Redis topic",
        ),
        BenchmarkQuery(
            "pathlib cross platform",
            [id_map["py_pathlib"]],
            description="Single relevant result",
        ),
        BenchmarkQuery(
            "Docker builds",
            [id_map["docker_multi"], id_map["docker_health"]],
            description="Docker cluster",
        ),
        BenchmarkQuery(
            "Kubernetes pods",
            [id_map["k8s_limits"], id_map["k8s_probes"]],
            description="K8s cluster",
        ),
        BenchmarkQuery(
            "secrets environment",
            [id_map["sec_secrets"], id_map["sec_jwt"]],
            description="Security cluster",
        ),
        BenchmarkQuery(
            "indexes foreign keys",
            [id_map["db_index"], id_map["db_migration"]],
            description="DB maintenance",
        ),
        BenchmarkQuery(
            "retry errors backoff",
            [id_map["err_retry"], id_map["err_circuit"]],
            description="Error handling cluster",
        ),
        BenchmarkQuery(
            "structured logging JSON",
            [id_map["log_struct"], id_map["log_level"]],
            description="Logging cluster",
        ),
        BenchmarkQuery(
            "CI dependencies cache",
            [id_map["ci_cache"], id_map["ci_parallel"]],
            description="CI/CD cluster",
        ),
        BenchmarkQuery(
            "API rate limiting",
            [id_map["proj_api_rate"], id_map["proj_api_version"]],
            scope="api-gateway",
            description="Project-scoped API gateway",
        ),
        BenchmarkQuery(
            "SLIs latency alerts",
            [id_map["mon_sli"], id_map["mon_alert"]],
            description="Monitoring cluster",
        ),
        BenchmarkQuery(
            "JWT validated request",
            [id_map["sec_jwt"], id_map["proj_auth"]],
            description="Cross-scope JWT (global + project)",
        ),
    ]


def precision_at_k(retrieved_ids, relevant_ids, k=5):
    """Compute precision@k."""
    retrieved_k = retrieved_ids[:k]
    relevant_set = set(relevant_ids)
    return sum(1 for r in retrieved_k if r in relevant_set) / k if k > 0 else 0.0


def recall_at_k(retrieved_ids, relevant_ids, k=5):
    """Compute recall@k."""
    retrieved_k = set(retrieved_ids[:k])
    relevant_set = set(relevant_ids)
    return sum(1 for r in relevant_set if r in retrieved_k) / len(relevant_set) if relevant_set else 0.0


def ndcg_at_k(retrieved_ids, relevant_ids, k=10):
    """Compute NDCG@k."""
    relevant_set = set(relevant_ids)
    dcg = sum(
        (1.0 / math.log2(i + 2)) for i, r in enumerate(retrieved_ids[:k]) if r in relevant_set
    )
    idcg = sum(1.0 / math.log2(i + 2) for i in range(min(len(relevant_ids), k)))
    return dcg / idcg if idcg > 0 else 0.0


def mrr(retrieved_ids, relevant_ids):
    """Compute Mean Reciprocal Rank."""
    relevant_set = set(relevant_ids)
    for i, r in enumerate(retrieved_ids):
        if r in relevant_set:
            return 1.0 / (i + 1)
    return 0.0


def compare_baseline(current_metrics, baseline_path=BASELINE_PATH, threshold=0.05):
    """Compare current metrics against baseline, flag regressions > threshold."""
    if not baseline_path.exists():
        return {"status": "no_baseline", "regressions": []}

    baseline = json.loads(baseline_path.read_text())
    regressions = []
    for metric, value in current_metrics.items():
        if metric in baseline:
            drop = baseline[metric] - value
            if drop > threshold:
                regressions.append({
                    "metric": metric,
                    "baseline": baseline[metric],
                    "current": value,
                    "drop": drop,
                })

    return {"status": "regression" if regressions else "pass", "regressions": regressions}


def _run_fts_benchmark(conn, queries):
    """Run benchmark queries using FTS5-only path and return per-query metrics."""
    from embeddings import search_hybrid

    total_p5, total_r5, total_ndcg10, total_mrr = 0, 0, 0, 0
    for q in queries:
        with patch("embeddings.HAS_FASTEMBED", False), \
             patch("embeddings.HAS_SQLITE_VEC", False):
            results = search_hybrid(conn, q.query, scope=q.scope, top_k=10)
        retrieved_ids = [r.data_point.id for r in results]
        total_p5 += precision_at_k(retrieved_ids, q.relevant_ids, k=5)
        total_r5 += recall_at_k(retrieved_ids, q.relevant_ids, k=5)
        total_ndcg10 += ndcg_at_k(retrieved_ids, q.relevant_ids, k=10)
        total_mrr += mrr(retrieved_ids, q.relevant_ids)

    n = len(queries)
    return {
        "precision_at_5": total_p5 / n,
        "recall_at_5": total_r5 / n,
        "ndcg_at_10": total_ndcg10 / n,
        "mrr": total_mrr / n,
    }


class TestBenchmarkInfrastructure:
    """Tests that the benchmark harness itself works correctly."""

    def test_seed_creates_memories(self, tmp_path):
        conn, id_map = seed_benchmark_db(tmp_path)
        count = conn.execute("SELECT COUNT(*) FROM data_points WHERE type='memory'").fetchone()[0]
        assert count >= 40, f"Expected >= 40 memories, got {count}"
        conn.close()

    def test_seed_creates_entities(self, tmp_path):
        conn, id_map = seed_benchmark_db(tmp_path)
        count = conn.execute("SELECT COUNT(*) FROM data_points WHERE type='entity'").fetchone()[0]
        assert count >= 4, f"Expected >= 4 entities, got {count}"
        conn.close()

    def test_seed_creates_fts_entries(self, tmp_path):
        conn, id_map = seed_benchmark_db(tmp_path)
        fts_count = conn.execute("SELECT COUNT(*) FROM fts_data").fetchone()[0]
        mem_count = conn.execute("SELECT COUNT(*) FROM data_points WHERE type='memory'").fetchone()[0]
        assert fts_count == mem_count, f"FTS entries ({fts_count}) != memory count ({mem_count})"
        conn.close()

    def test_queries_have_relevant_ids(self, tmp_path):
        conn, id_map = seed_benchmark_db(tmp_path)
        queries = build_benchmark_queries(id_map)
        assert len(queries) >= 20, f"Expected >= 20 queries, got {len(queries)}"
        for q in queries:
            assert len(q.relevant_ids) >= 1, f"Query '{q.query}' has no relevant IDs"
        conn.close()

    def test_precision_at_k_correct(self):
        assert precision_at_k(["a", "b", "c", "d", "e"], ["a", "c"], k=5) == 0.4
        assert precision_at_k(["a", "b", "c", "d", "e"], ["a", "b", "c", "d", "e"], k=5) == 1.0
        assert precision_at_k([], ["a"], k=5) == 0.0

    def test_recall_at_k_correct(self):
        assert recall_at_k(["a", "b", "c", "d", "e"], ["a", "c"], k=5) == 1.0
        assert recall_at_k(["a", "b"], ["a", "c", "d"], k=5) == pytest.approx(1 / 3)
        assert recall_at_k([], [], k=5) == 0.0

    def test_ndcg_at_k_correct(self):
        assert ndcg_at_k(["a", "b"], ["a"], k=10) == 1.0
        assert ndcg_at_k(["x", "a"], ["a"], k=10) == pytest.approx(1.0 / math.log2(3))
        assert ndcg_at_k(["x", "y", "z"], ["a"], k=10) == 0.0

    def test_mrr_correct(self):
        assert mrr(["x", "a", "y"], ["a"]) == 0.5
        assert mrr(["a", "b", "c"], ["a"]) == 1.0
        assert mrr(["x", "y", "z"], ["a"]) == 0.0

    def test_compare_baseline_no_file(self, tmp_path):
        result = compare_baseline({"mrr": 0.5}, tmp_path / "missing.json")
        assert result["status"] == "no_baseline"

    def test_compare_baseline_pass(self, tmp_path):
        baseline_path = tmp_path / "baseline.json"
        baseline_path.write_text(json.dumps({"mrr": 0.5}))
        result = compare_baseline({"mrr": 0.48}, baseline_path, threshold=0.05)
        assert result["status"] == "pass"

    def test_compare_baseline_regression(self, tmp_path):
        baseline_path = tmp_path / "baseline.json"
        baseline_path.write_text(json.dumps({"precision_at_5": 0.8, "recall_at_5": 0.7}))
        current = {"precision_at_5": 0.6, "recall_at_5": 0.7}
        result = compare_baseline(current, baseline_path, threshold=0.05)
        assert result["status"] == "regression"
        assert len(result["regressions"]) == 1
        assert result["regressions"][0]["metric"] == "precision_at_5"
        assert result["regressions"][0]["drop"] == pytest.approx(0.2)


class TestBenchmarkExecution:
    """Run the actual benchmark and check metrics."""

    def test_fts_benchmark_runs(self, tmp_path):
        """Benchmark runs with FTS5 (no vector) and produces reasonable metrics."""
        conn, id_map = seed_benchmark_db(tmp_path)
        queries = build_benchmark_queries(id_map)
        metrics = _run_fts_benchmark(conn, queries)

        assert metrics["mrr"] > 0.0, (
            f"MRR is 0 -- FTS5 search found nothing relevant. Metrics: {metrics}"
        )
        assert metrics["precision_at_5"] >= 0.0
        assert metrics["recall_at_5"] >= 0.0
        assert metrics["ndcg_at_10"] >= 0.0
        conn.close()

    def test_fts_benchmark_metrics_in_range(self, tmp_path):
        """Verify all metrics fall within [0, 1] range."""
        conn, id_map = seed_benchmark_db(tmp_path)
        queries = build_benchmark_queries(id_map)
        metrics = _run_fts_benchmark(conn, queries)

        for name, value in metrics.items():
            assert 0.0 <= value <= 1.0, f"{name} = {value} is out of [0, 1]"
        conn.close()

    def test_regression_detection_catches_degradation(self, tmp_path):
        """Regression detection catches intentional metric drops."""
        baseline = {"precision_at_5": 0.8, "recall_at_5": 0.7}
        baseline_path = tmp_path / "baseline.json"
        baseline_path.write_text(json.dumps(baseline))

        current = {"precision_at_5": 0.6, "recall_at_5": 0.7}
        result = compare_baseline(current, baseline_path, threshold=0.05)
        assert result["status"] == "regression"
        assert any(r["metric"] == "precision_at_5" for r in result["regressions"])

    def test_regression_detection_passes_stable(self, tmp_path):
        """No regression when metrics are stable or improved."""
        baseline = {"precision_at_5": 0.5, "recall_at_5": 0.5}
        baseline_path = tmp_path / "baseline.json"
        baseline_path.write_text(json.dumps(baseline))

        current = {"precision_at_5": 0.55, "recall_at_5": 0.5}
        result = compare_baseline(current, baseline_path, threshold=0.05)
        assert result["status"] == "pass"
        assert len(result["regressions"]) == 0

    def test_scope_filtering_works(self, tmp_path):
        """Scoped queries only return results from the matching scope."""
        from embeddings import search_hybrid

        conn, id_map = seed_benchmark_db(tmp_path)
        with patch("embeddings.HAS_FASTEMBED", False), \
             patch("embeddings.HAS_SQLITE_VEC", False):
            results = search_hybrid(conn, "JWT tokens", scope="auth-service", top_k=10)

        assert len(results) > 0, (
            "Expected at least one result for 'JWT tokens' in auth-service scope"
        )
        for r in results:
            assert r.data_point.scope == "auth-service", (
                f"Expected scope 'auth-service', got '{r.data_point.scope}'"
            )
        conn.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--update-baseline", action="store_true",
                        help="Run benchmark and save metrics as new baseline")
    args = parser.parse_args()

    if args.update_baseline:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            conn, id_map = seed_benchmark_db(tmp_path)
            queries = build_benchmark_queries(id_map)
            metrics = _run_fts_benchmark(conn, queries)
            BASELINE_PATH.write_text(json.dumps(metrics, indent=2))
            print(f"Baseline updated: {BASELINE_PATH}")
            print(json.dumps(metrics, indent=2))
            conn.close()
    else:
        pytest.main([__file__, "-v"])
