from __future__ import annotations

import hashlib
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Shared utilities — use these in both ResponseCache and SharedRedisCache
# ---------------------------------------------------------------------------

PRIVACY_PATTERNS = re.compile(
    # --- Tài chính / tài khoản ---
    r"\b(balance|credit[\s\-]?card|bank[\s\-]?account|routing[\s\-]?number"
    r"|account[\s\-]?number|transaction|wire[\s\-]?transfer|loan|debt|statement)\b"
    # --- Xác thực / bí mật ---
    r"|\b(password|passwd|\bpin\b|api[\s\-]?key|secret[\s\-]?key|access[\s\-]?token"
    r"|auth[\s\-]?code|otp|2fa|two[\s\-]?factor)\b"
    # --- Giấy tờ tùy thân ---
    r"|\b(ssn|social[\s\-]?security|passport[\s\-]?number|driver[\s\-]?licen[sc]e"
    r"|national[\s\-]?id|cmnd|cccd|citizen[\s\-]?id)\b"
    # --- Dữ liệu cá nhân ---
    r"|\b(medical[\s\-]?record|diagnosis|prescription|health[\s\-]?data"
    r"|date[\s\-]?of[\s\-]?birth|\bdob\b|home[\s\-]?address)\b"
    # --- Dữ liệu theo user cụ thể: "for user/customer/employee 123" ---
    r"|\b(user|account|customer|employee|student|client|member)\s*[#:]?\s*\d+"
    r"|\bfor\s+(user|account|customer|employee|student|client|member)\s+\d+"
    # --- Số điện thoại ---
    r"|0[3-9]\d{8}\b"                    # VN: 09x, 03x, 07x...
    r"|\+84[3-9]\d{8}\b"                 # VN quốc tế: +849x
    r"|\+[1-9]\d{7,14}\b"               # Quốc tế chung
    r"|\b\d{3}[\s\-\.]\d{3}[\s\-\.]\d{4}\b"  # US format: 123-456-7890
    # --- Email ---
    r"|[\w.+\-]+@[\w\-]+\.[a-zA-Z]{2,}"
    # --- Số ID dài (CMND 9 số, CCCD 12 số) ---
    r"|\b\d{9}\b|\b\d{12}\b"
    # --- Tiếng Việt ---
    r"|(tên\s+(tôi|mình|em|anh|chị|tao)\s+là)"
    r"|(số\s+(điện\s+thoại|đt|phone)\s*(tôi|mình|em|anh|chị|của\s+tôi)?)"
    r"|(địa\s+chỉ\s+(tôi|mình|em|nhà|của\s+tôi))",
    re.IGNORECASE,
)


def _is_uncacheable(query: str) -> bool:
    """Return True if query contains privacy-sensitive keywords."""
    return bool(PRIVACY_PATTERNS.search(query))


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True if query and cached key contain different significant numbers.

    Checks two tiers:
    - 4-digit numbers: years (2024 vs 2026), IDs (1001 vs 1002)
    - 3-digit numbers: version codes, semester codes, etc.
    A mismatch at either tier signals the cached answer is for a different context.
    """
    # Tier 1: 4-digit numbers (years, IDs) — highest signal
    nums4_q = set(re.findall(r"\b\d{4}\b", query))
    nums4_c = set(re.findall(r"\b\d{4}\b", cached_key))
    if nums4_q and nums4_c and nums4_q != nums4_c:
        return True
    # Tier 2: 3-digit numbers (e.g. "course 101" vs "course 202")
    nums3_q = set(re.findall(r"\b\d{3}\b", query))
    nums3_c = set(re.findall(r"\b\d{3}\b", cached_key))
    if nums3_q and nums3_c and nums3_q != nums3_c:
        return True
    return False


# ---------------------------------------------------------------------------
# In-memory cache (existing)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """Simple in-memory cache skeleton.

    TODO(student): Add a better semantic similarity function and false-hit guardrails.
    Use the module-level _is_uncacheable() and _looks_like_false_hit() helpers in your
    get() and set() methods.  For production, replace with SharedRedisCache.
    """

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []
        self.false_hit_log: list[dict[str, object]] = []

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response by semantic similarity.

        TODO(student): Implement cache lookup with guardrails:
        1. Return (None, 0.0) if _is_uncacheable(query) — privacy check
        2. Evict expired entries (compare time.time() - created_at vs ttl_seconds)
        3. Find best matching entry using self.similarity(query, entry.key)
        4. If best_score >= similarity_threshold:
           a. Check _looks_like_false_hit(query, best_key) — if true, log to
              self.false_hit_log and return (None, best_score)
           b. Otherwise return (best_value, best_score)
        5. Return (None, best_score) if no match above threshold

        You'll need a self.false_hit_log: list[dict[str, object]] attribute
        (add it in __init__).
        """
        if _is_uncacheable(query):
            return None, 0.0

        # Xóa các entry đã hết hạn
        now = time.time()
        self._entries = [e for e in self._entries if now - e.created_at <= self.ttl_seconds]

        # Tìm entry có similarity cao nhất
        best_score = 0.0
        best_entry: CacheEntry | None = None
        for entry in self._entries:
            score = self.similarity(query, entry.key)
            if score > best_score:
                best_score = score
                best_entry = entry

        if best_entry is not None and best_score >= self.similarity_threshold:
            if _looks_like_false_hit(query, best_entry.key):
                self.false_hit_log.append({"query": query, "cached_key": best_entry.key, "score": best_score, "reason": "date_or_number_mismatch"})
                return None, best_score
            return best_entry.value, best_score

        return None, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response in cache.

        TODO(student): Implement with privacy guardrail:
        1. Return immediately if _is_uncacheable(query)
        2. Append a CacheEntry to self._entries
        """
        if _is_uncacheable(query):
            return
        self._entries.append(CacheEntry(
            key=query,
            value=value,
            created_at=time.time(),
            metadata=metadata or {},
        ))

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """Compute semantic similarity between two strings.

        TODO(student): Implement cosine similarity over character n-grams + word tokens.
        The naive token-overlap (Jaccard) approach loses too much information.

        Suggested approach:
        1. If a == b, return 1.0
        2. Tokenize both strings: split into words + character n-grams (n=3)
           e.g., "hello world" → ["hello", "world", "hel", "ell", "llo", "wor", "orl", "rld"]
        3. Build Counter (bag-of-words) vectors from these tokens
        4. Compute cosine similarity: dot(a,b) / (|a| * |b|)

        Hint: Use collections.Counter and math.sqrt.
        Import them at the top of the file.
        """
        if a == b:
            return 1.0

        def tokenize(s: str) -> list[str]:
            words = s.lower().split()
            ngrams = [s.lower()[i:i+3] for i in range(len(s.lower()) - 2)]
            return words + ngrams

        va = Counter(tokenize(a))
        vb = Counter(tokenize(b))

        dot = sum(va[t] * vb[t] for t in va if t in vb)
        mag_a = math.sqrt(sum(v * v for v in va.values()))
        mag_b = math.sqrt(sum(v * v for v in vb.values()))

        if mag_a == 0 or mag_b == 0:
            return 0.0
        return dot / (mag_a * mag_b)


# ---------------------------------------------------------------------------
# Redis shared cache (new)
# ---------------------------------------------------------------------------


class SharedRedisCache:
    """Redis-backed shared cache for multi-instance deployments.

    TODO(student): Implement the get() and set() methods using Redis commands
    so that cache state is shared across multiple gateway instances.

    Data model (suggested):
        Key    = "{prefix}{query_hash}"   (Redis String namespace)
        Value  = Redis Hash with fields:  "query", "response"
        TTL    = Redis EXPIRE (automatic cleanup — no manual eviction)

    For similarity lookup: SCAN all keys with self.prefix, HGET each entry's
    "query" field, compute similarity locally via ResponseCache.similarity().

    Provided helpers:
        _is_uncacheable(query)          — True if privacy-sensitive
        _looks_like_false_hit(q, key)   — True if 4-digit numbers differ
        self._query_hash(query)         — deterministic short hash for Redis key
        ResponseCache.similarity(a, b)  — reuse your improved similarity function
    """

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
    ):
        import redis as redis_lib

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        self._redis: Any = redis_lib.Redis.from_url(redis_url, decode_responses=True)
        self._fallback: ResponseCache = ResponseCache(ttl_seconds, similarity_threshold)

    def ping(self) -> bool:
        """Check Redis connectivity."""
        try:
            return bool(self._redis.ping())
        except Exception:
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response from Redis.

        TODO(student): Implement cache lookup.  Suggested steps:
        1. Return (None, 0.0) if _is_uncacheable(query)
        2. Build exact-match key: f"{self.prefix}{self._query_hash(query)}"
        3. Try self._redis.hget(key, "response") — if found return (response, 1.0)
        4. Otherwise self._redis.scan_iter(f"{self.prefix}*") to iterate all cached keys
        5. For each key, HGET "query" field and compute
           ResponseCache.similarity(query, cached_query)
        6. Track best match that is >= self.similarity_threshold
        7. Before returning a match, check _looks_like_false_hit(); if true,
           append to self.false_hit_log and return (None, best_score)
        """
        if _is_uncacheable(query):
            return None, 0.0

        try:
            return self._redis_get(query)
        except Exception:
            return self._fallback.get(query)

    def _redis_get(self, query: str) -> tuple[str | None, float]:
        # Exact match trước
        exact_key = f"{self.prefix}{self._query_hash(query)}"
        response = self._redis.hget(exact_key, "response")
        if response is not None:
            return response, 1.0

        # Similarity scan toàn bộ keys
        best_score = 0.0
        best_response: str | None = None
        best_cached_query: str | None = None

        for key in self._redis.scan_iter(f"{self.prefix}*"):
            cached_query = self._redis.hget(key, "query")
            if cached_query is None:
                continue
            score = ResponseCache.similarity(query, cached_query)
            if score > best_score:
                best_score = score
                best_cached_query = cached_query
                best_response = self._redis.hget(key, "response")

        if best_response is not None and best_score >= self.similarity_threshold:
            if _looks_like_false_hit(query, best_cached_query or ""):
                self.false_hit_log.append({"query": query, "cached_key": best_cached_query, "score": best_score, "reason": "date_or_number_mismatch"})
                return None, best_score
            return best_response, best_score

        return None, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        if _is_uncacheable(query):
            return
        try:
            self._redis_set(query, value)
        except Exception:
            self._fallback.set(query, value, metadata)
        return

    def _redis_set(self, query: str, value: str) -> None:
        """Store a response in Redis with TTL.

        TODO(student): Implement cache storage.  Suggested steps:
        1. Return immediately if _is_uncacheable(query)
        2. Build key: f"{self.prefix}{self._query_hash(query)}"
        3. self._redis.hset(key, mapping={"query": query, "response": value})
        4. self._redis.expire(key, self.ttl_seconds)
        """
        if _is_uncacheable(query):
            return
        key = f"{self.prefix}{self._query_hash(query)}"
        self._redis.hset(key, mapping={"query": query, "response": value})
        self._redis.expire(key, self.ttl_seconds)

    def flush(self) -> None:
        """Remove all entries with this cache prefix (for testing)."""
        for key in self._redis.scan_iter(f"{self.prefix}*"):
            self._redis.delete(key)

    def close(self) -> None:
        """Close Redis connection."""
        if self._redis is not None:
            self._redis.close()

    @staticmethod
    def _query_hash(query: str) -> str:
        """Deterministic short hash for a query string."""
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]
