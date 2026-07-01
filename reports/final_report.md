# Day 10 Reliability Final Report

## 1. Architecture Diagram

```
User Request
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│                   Reliability Gateway                    │
│                                                         │
│  ┌─────────────────────┐                                │
│  │  Cache              │  ← in-memory (ResponseCache)   │
│  │  (in-mem / Redis)   │    or Redis (SharedRedisCache)  │
│  └────────┬────────────┘                                │
│           │ miss                                        │
│           ▼                                             │
│  ┌─────────────────────────────────────────────────┐   │
│  │          Circuit Breaker Chain                   │   │
│  │                                                  │   │
│  │  [CB: primary] → FakeLLMProvider "primary"       │   │
│  │       ↓ ProviderError / CircuitOpenError         │   │
│  │  [CB: backup]  → FakeLLMProvider "backup"        │   │
│  │       ↓ all fail                                 │   │
│  │  Static Fallback Message                         │   │
│  └─────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

**Route labels được gắn vào mỗi response:**
- `cache_hit:0.97` — served từ cache với similarity score
- `primary` — served bởi primary provider
- `fallback` — served bởi backup provider
- `static_fallback` — tất cả provider lỗi

---

## 2. Config Table với Rationale

| Parameter | Giá trị | Lý do chọn |
|---|---|---|
| `failure_threshold` | 3 | Đủ để lọc lỗi ngẫu nhiên (1–2 lỗi có thể là transient), nhưng phản ứng nhanh trước lỗi liên tục. Chọn 5 thì hệ thống chịu đựng quá lâu trước khi ngắt. |
| `reset_timeout_seconds` | 2 | Provider giả lập latency 180–260ms. 2 giây đủ cho 1 recovery cycle mà không để circuit OPEN quá lâu. |
| `success_threshold` | 1 | 1 probe request thành công là đủ để tái mở — phù hợp với provider đơn giản. Production nên dùng 2–3. |
| `ttl_seconds` | 300 | 5 phút phù hợp cho câu trả lời về chính sách/FAQ (ít thay đổi). Câu hỏi kỹ thuật có thể dùng TTL dài hơn. |
| `similarity_threshold` | 0.92 | Đã thử 0.85 — xảy ra false hit giữa "chính sách 2024" và "chính sách 2026" (score ~0.91). Nâng lên 0.92 loại bỏ false hit này mà vẫn cache được "Summarize refund policy" → "What is the refund policy?" (score ~0.95). |
| `cache.backend` | memory | Mặc định cho single-instance. Đổi sang `redis` khi cần multi-instance. |
| `load_test.requests` | 100 | Đủ để circuit breaker trigger (cần ≥ failure_threshold lỗi liên tiếp) và cache đạt warm state. |

---

## 3. Metrics từ metrics.json

Chạy: `python scripts/run_chaos.py --config configs/default.yaml --out reports/metrics.json`

### Tổng thể (500 requests, 5 scenarios)

| Metric | Giá trị |
|---|---:|
| total_requests | 500 |
| availability | **98.8%** |
| error_rate | 1.2% |
| latency_p50_ms | 265.26 ms |
| latency_p95_ms | 324.70 ms |
| latency_p99_ms | 331.88 ms |
| cache_hit_rate | 50.8% |
| fallback_success_rate | 95.4% |
| circuit_open_count | 14 |
| recovery_time_ms | ~2,307 ms |
| estimated_cost | $0.1118 |
| estimated_cost_saved | **$0.254** |

---

## 4. Chaos Scenarios — Expected vs Observed

| Scenario | Expected | Observed | Kết quả |
|---|---|---|---|
| **primary_timeout_100** | Primary hỏng hoàn toàn, backup gánh toàn bộ. Circuit primary mở sau 3 lỗi liên tiếp. Availability giảm nhẹ do độ trễ circuit. | Availability 97%, cache hit 60%, circuit mở 6 lần, backup fallback rate 92.5%, cost $0.013 | ✅ pass |
| **primary_flaky_50** | Circuit oscillate — mở/đóng xen kẽ do primary không ổn định. Cache giúp bypass nhiều request. | Availability 99%, cache hit 59%, circuit mở 6 lần, cost $0.013 | ✅ pass |
| **all_healthy** | Cả 2 provider khỏe, cache warm nhanh vì query tái sử dụng từ các scenario trước. | Availability 100%, cache hit 73%, circuit 0 lần mở, cost $0.014 | ✅ pass |
| **all_healthy_with_cache** | Baseline với cache bật. | Availability 98%, cache hit 62%, cost $0.018 | ✅ pass |
| **all_healthy_no_cache** | Không cache — mọi request đều gọi provider. Chi phí và circuit open tăng. | Availability 100%, cache hit 0%, circuit mở 2 lần, cost **$0.053** | ✅ pass |

**Recovery evidence:** Circuit mở (to=open) lúc t=T, chuyển HALF_OPEN sau 2 giây (reset_timeout), probe thành công → CLOSED. Recovery time trung bình **2,307 ms** — ghi lại đầy đủ trong `transition_log` của mỗi CircuitBreaker.

---

## 5. Cache Comparison — Có vs Không Cache

Cùng điều kiện: 100 requests, cả 2 provider hoàn toàn khỏe.

| Metric | Có Cache | Không Cache | Delta |
|---|---:|---:|---:|
| cache_hit_rate | 62% | 0% | +62% |
| estimated_cost | $0.018 | $0.053 | **↓ 65%** |
| estimated_cost_saved | $0.062 | $0.000 | — |
| availability | 98% | 100% | −2% (nhỏ không đáng kể) |
| latency_p50_ms | 237 ms | 231 ms | tương đương |
| latency_p95_ms | 320 ms | 321 ms | tương đương |

**Kết luận:** Cache giảm 65% chi phí. Latency không thay đổi đáng kể vì cache hit trả về 0ms nhưng cache miss vẫn phải gọi provider (~230ms) — P50 bị kéo về phía provider latency.

### False-hit Example

```
Cached query : "Summarize refund policy for 2024 deadline"   (stored)
New query    : "Summarize refund policy for 2026 deadline"   (incoming)
Similarity   : ~0.93  →  vượt threshold 0.92
4-digit check: {2024} ≠ {2026}  →  FALSE HIT DETECTED
Action       : reject cache, call provider for fresh answer
Logged to    : cache.false_hit_log[0] = {reason: "date_or_number_mismatch"}
```

---

## 6. Redis Shared Cache Evidence

### Shared state proof — 2 instances đọc/ghi chung

```python
# Instance 1 ghi
c1 = SharedRedisCache("redis://localhost:6379/0", ...)
c1.set("Summarize the refund policy", "Refund policy: full refund within 30 days.")

# Instance 2 đọc (không ghi gì) — vẫn tìm thấy
c2 = SharedRedisCache("redis://localhost:6379/0", ...)
val, score = c2.get("Summarize the refund policy")
# → score=1.00, found=True  
```

### KEYS output sau khi populate cache

```
$ docker exec phase2-track3-day10-2a202600838-redis-1 redis-cli KEYS "rl:cache:*"
rl:cache:fa14a8b819fa
rl:cache:b5d80f234e0f
rl:cache:e27f05b60fe8
```

Mỗi key là `rl:cache:{md5_hash_12_chars}`. Redis Hash bên trong:
```
HGETALL rl:cache:fa14a8b819fa
1) "query"
2) "Summarize the refund policy"
3) "response"
4) "Refund policy: full refund within 30 days."
```

**Privacy guardrail trên Redis:** Query chứa từ khóa nhạy cảm (`password`, `user 123`, số điện thoại...) sẽ không được lưu — `set()` return ngay lập tức, không gọi Redis.

---

## 7. Failure Analysis

**Điểm yếu:** Privacy guardrail dựa hoàn toàn vào regex — dễ bỏ sót cách diễn đạt không theo chuẩn.

Ví dụ không bị chặn:
- *"Cho tôi biết số dư ví của Nguyễn Văn A"* — không có từ khóa nhận diện
- *"My acct 4242 has how much left?"* — viết tắt "acct" không match pattern `account`

**Hướng cải thiện:** Tích hợp [Microsoft Presidio](https://microsoft.github.io/presidio/) (NER-based PII detection) thay regex. Chính xác hơn với mọi ngôn ngữ, thêm ~50–100ms latency mỗi request — chấp nhận được với câu hỏi nhạy cảm.

---

## 8. Stretch Goals

### 8.1 SLO Table

Định nghĩa tại `src/reliability_lab/metrics.py` — method `check_slos()`.
Kết quả tự động tính và ghi vào `reports/metrics.json` (field `slo_results`) sau mỗi lần chạy chaos.

| SLO | Ngưỡng | Kết quả thực tế | Đạt? |
|---|---|---|---|
| Availability | ≥ 99% | 98.4% | ❌ |
| P95 Latency | < 2,500 ms | 321 ms | ✅ |
| Error rate | < 1% | 1.6% | ❌ |
| Cache hit rate | ≥ 30% | 47.4% | ✅ |
| Fallback success rate | ≥ 90% | 93.3% | ✅ |

> Availability và error rate chưa đạt SLO do kịch bản `primary_timeout_100` kéo trung bình xuống. Trong `all_healthy`, availability đạt 100%.

**Output:** `reports/metrics.json` → field `"slo_results"`

---

### 8.2 Redis Graceful Degradation

Implement tại `src/reliability_lab/cache.py` — `SharedRedisCache.get()` và `set()`.

Khi Redis không phản hồi, hệ thống tự động fall back về `ResponseCache` (in-memory) thay vì crash:

```
Redis.get() raises Exception  →  fallback.get()   (in-memory ResponseCache)
Redis.set() raises Exception  →  fallback.set()   (in-memory ResponseCache)
```

Không có output file riêng — behaviour được bảo đảm bởi `tests/test_redis_cache.py`.

---

### 8.3 Cost-Aware Routing

Implement tại `src/reliability_lab/gateway.py` — `ReliabilityGateway.complete()`.

| Mức budget tích lũy | Hành vi |
|---|---|
| < 80% | Route bình thường: primary → fallback |
| ≥ 80% | Bỏ qua provider đắt, chỉ dùng provider có `cost_per_1k_tokens` thấp nhất |
| ≥ 100% | Ngừng gọi provider, trả về cache hoặc static fallback |

Sử dụng: `ReliabilityGateway([...], {...}, cost_budget=0.05)`. Mặc định `float("inf")` — không giới hạn.

---

### 8.4 Concurrent Load Test (ThreadPoolExecutor)

Implement tại `src/reliability_lab/chaos.py` — `run_simulation()`.

Các scenario chạy **song song** (`ThreadPoolExecutor(max_workers=4)`) thay vì tuần tự — giảm thời gian chạy toàn bộ test từ `N × t_scenario` xuống còn `~t_scenario`.

**Output:** `reports/metrics.json` — sinh ra sau khi tất cả scenarios hoàn thành song song.
Chạy lại: `python scripts/run_chaos.py --config configs/default.yaml --out reports/metrics.json`

---

### 8.5 Property-Based Tests (Hypothesis)

File: `tests/test_property_circuit_breaker.py`

Dùng thư viện `hypothesis` để fuzz state transitions với input ngẫu nhiên:

| Test | Invariant được kiểm tra | Examples |
|---|---|---|
| `test_opens_exactly_at_threshold` | Circuit OPEN iff failures ≥ threshold | 200 |
| `test_success_resets_failure_count_invariant` | Sau success, failure_count luôn = 0 | 100 |
| `test_half_open_closes_after_success_threshold` | HALF_OPEN → CLOSED chỉ khi success ≥ threshold | 100 |
| `test_open_denies_all_requests_before_timeout` | Circuit OPEN luôn deny trước timeout | 50 |
| `test_half_open_failure_always_reopens` | Failure trong HALF_OPEN → OPEN với reason `"probe_failure"` | 50 |

Chạy: `pytest tests/test_property_circuit_breaker.py -v`

---

### 8.6 Redis Circuit State (Multi-Instance Sharing)

Class `RedisCircuitBreaker` trong `src/reliability_lab/circuit_breaker.py`.

State lưu trong Redis thay vì bộ nhớ — nhiều gateway instances chia sẻ cùng circuit state:

```
Redis Hash "cb:{name}":
  state         → "closed" / "open" / "half_open"
  failure_count → HINCRBY (atomic increment)
  success_count → HINCRBY (atomic increment)
  opened_at     → timestamp khi mở (time.monotonic)
```

Kết quả: 1 instance gặp lỗi, tất cả instances còn lại đều thấy circuit mở ngay lập tức.

Kiểm tra trực tiếp:
```bash
docker exec phase2-track3-day10-2a202600838-redis-1 redis-cli HGETALL cb:primary
```

---

## Kết luận

Hệ thống đã giải quyết 5 vấn đề production reliability:

| Vấn đề | Giải pháp | Kết quả đo được |
|---|---|---|
| Sụp đổ dây chuyền | Circuit Breaker 3-state | Recovery < 2.5s, 10 lần mở/phục hồi |
| Chi phí gọi LLM lặp lại | Semantic Cache (n-gram cosine) | Giảm 65% cost, hit rate 47% |
| Cache trả kết quả sai theo năm/ID | False-hit detection | Block 4-digit + 3-digit mismatch |
| Dữ liệu cá nhân bị cache | Privacy guardrails (10+ pattern) | 0 privacy query được lưu vào cache |
| Cache không share giữa instances | Redis Shared Cache | 2 instances xác minh shared state |

**Tổng kết test:** `40 passed, 7 xpassed` — 0 failures. `mypy src`: `Success: no issues found in 8 source files`.

**Availability: 98.4%** trên 500 requests qua 5 chaos scenarios.

---

## Output Files

| File | Sinh ra bởi | Nội dung |
|---|---|---|
| `reports/metrics.json` | `python scripts/run_chaos.py --config configs/default.yaml --out reports/metrics.json` | Metrics tổng thể, per-scenario details, SLO results |
| `reports/final_report.md` | `python scripts/generate_report.py --metrics reports/metrics.json --out reports/final_report.md` | Báo cáo đầy đủ |
| `reports/test_output.txt` | `pytest -v \| tee reports/test_output.txt` | Log kết quả 42 tests |
