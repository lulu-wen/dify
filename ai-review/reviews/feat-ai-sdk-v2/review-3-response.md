# Review Response: feat-ai-sdk-v2 — Round 3

> Response to `reviews/feat-ai-sdk-v2/review-3.md`.

## Summary

| 嚴重度 | 找到 | 已修 | 不修 |
|---|---|---|---|
| [P1] | 0 | — | — |
| [P2] | 2 | 2 | 0 |

Round 3 收斂結果：0 P1，2 個 P2 全修。**這兩個 P2 都是 round-2 修法的
second-order bug** — 修了「上游 4xx 不能壓成 502」，但忘記區分「哪種 4xx
真的是客戶錯」；修了 happy path，但忘記「2xx 也可能 body 不對」。同一個
pattern 在 PR #1 round-2 出現過，這次又出現一次，是值得記下來的 process
教訓（已存進記憶）。

## Findings 處理紀錄

---

### Finding 1: [P2] Do not classify every upstream 4xx as invalid input

- **Severity**: [P2]
- **Codex 描述**:
  > When the registered embedding service returns 401/403 because
  > `model_entry.api_key` is wrong or expired (or 429 for upstream
  > throttling), those statuses come from the gateway's upstream
  > credential/service rather than from the SDK caller. This blanket
  > branch wraps them as `invalid_request_error` /
  > `upstream_invalid_request` and returns them to a caller with a valid
  > SDK key, so auth/config failures and rate limits look like bad client
  > input instead of upstream failures; only request-shape statuses such
  > as 400/413/422 should take this path.
- **影響檔案**: `gateway/src/gateway/embeddings/client.py:82-86`
- **動作**: ✅ Fixed

#### 驗證

Codex 完全是對的。Round-2 的 fix 把 4xx 一律當客戶錯，但實際上 4xx 有兩類：

| 4xx 狀態碼 | 誰的錯 | 我之前的處理 | 正確處理 |
|---|---|---|---|
| **400, 413, 422** | 客戶（request shape 不對） | `UpstreamClientError`（4xx 透傳） | 同上 ✓ |
| **401** | Gateway（上游 api_key 壞 / 過期） | `UpstreamClientError`（**錯**） | `DifyUpstreamError` (502) |
| **403** | Gateway（被上游 forbidden） | `UpstreamClientError`（**錯**） | `DifyUpstreamError` (502) |
| **404** | Gateway（上游不認得 served model） | `UpstreamClientError`（**錯**） | `DifyUpstreamError` (502) |
| **429** | Gateway（被上游 rate-limit） | `UpstreamClientError`（**錯**） | `DifyUpstreamError` (502) |

如果不修這個，會出現一個嚴重的誤導場景：

- 客戶用一個**完全正確**的 SDK key 打 Gateway
- Gateway 後台的 vllm-embed `api_key` 過期（這是運維 / 設定問題，跟客戶無關）
- 客戶收到 `HTTP 401 invalid_request_error / upstream_invalid_request`
- 客戶以為**自己**的 SDK key 失效，跑去 reset key、開 ticket → 浪費客服跟客戶時間

#### 修復內容

`gateway/src/gateway/embeddings/client.py`：

1. 加 module-level constant：
   ```python
   _REQUEST_SHAPE_STATUSES: frozenset[int] = frozenset({400, 413, 422})
   ```
   集中宣告「哪些 4xx 算客戶錯」，未來要加（例如 415 unsupported media）改一個地方。

2. 把粗略的 `if 400 <= resp.status_code < 500:` 改成精確 set membership：
   ```python
   if resp.status_code in _REQUEST_SHAPE_STATUSES:
       raise UpstreamClientError(...)
   raise DifyUpstreamError(...)
   ```
   其他 4xx（401/403/404/429/...）跟 5xx 都走同一條 `DifyUpstreamError` 路徑。

3. Docstring 把 `Raises:` 區塊明確寫出哪些 status 走哪條，未來 reviewer 不用 grep。

#### 測試

`gateway/tests/test_embeddings.py`：

- 既有 `test_embeddings_upstream_4xx_passes_through` 維持 `[400, 413, 422]` —
  確認 request-shape 4xx 仍然透傳。
- 新增 `test_embeddings_upstream_non_shape_4xx_becomes_502`，
  parametrized `[401, 403, 404, 429]` — 4 個 status 都驗證會包成 502
  `dify_upstream_error`。

---

### Finding 2: [P2] Convert malformed successful upstream bodies into upstream errors

- **Severity**: [P2]
- **Codex 描述**:
  > When a registered embedding backend responds with HTTP 2xx but
  > non-JSON content (for example an HTML error page from a proxy),
  > `resp.json()` raises here; similarly, a non-object JSON body later
  > fails at `upstream_response["model"]`. Those exceptions are not
  > `GatewayError`s, so this new endpoint returns an internal 500 instead
  > of the gateway's upstream-error envelope for an upstream failure.
- **影響檔案**: `gateway/src/gateway/embeddings/client.py:92`
- **動作**: ✅ Fixed

#### 驗證

兩個真實會發生的情境：

1. **2xx 但 body 是 HTML**：客戶端 vllm-embed 前面擋個 nginx / cloudflare，
   反向代理掛了會回 HTTP 200 + HTML error page（`<html><body>Bad Gateway</body></html>`），
   `resp.json()` 直接 raise `JSONDecodeError`。

2. **2xx 但 JSON 是 array / null / number**：上游被某種 middleware 改造後
   回了陣列，router 端 `upstream_response["model"] = body.model` 直接 raise
   `TypeError: list indices must be integers`。

兩個 exception 都不是 `GatewayError` 子類 → FastAPI 全域 handler 沒接到 →
client 收到 FastAPI 預設 500 Internal Server Error（非 OpenAI envelope）。
違反 R7 contract（所有錯誤都該是 OpenAI envelope）。

#### 修復內容

`gateway/src/gateway/embeddings/client.py:99-115`：

```python
# 2xx 但 body 可能是垃圾 — 用 try/except 包 resp.json()，再 isinstance check
try:
    parsed = resp.json()
except (json.JSONDecodeError, ValueError) as e:
    raise DifyUpstreamError(
        f"Embedding endpoint returned non-JSON body: {_truncate(resp.text)}"
    ) from e
if not isinstance(parsed, dict):
    raise DifyUpstreamError(
        f"Embedding endpoint returned non-object JSON ({type(parsed).__name__}): ..."
    )
return parsed
```

兩條防護：(a) JSON 解析失敗 → 502，(b) 解出來不是 dict → 502。
Router 端 `upstream_response["model"] = body.model` 不需要動，因為 client
端已經保證回來的一定是 dict。

import `json` 模組（之前 client.py 沒 import）。

#### 測試

- `test_embeddings_upstream_non_json_body_becomes_502` — 上游回 HTML 200
  → client 收到 502 + `error.code == "dify_upstream_error"` +
  message 含 `"non-JSON"` 字串。
- `test_embeddings_upstream_non_object_json_becomes_502` — 上游回 JSON
  array 200 → client 收到 502，message 含 `"non-object JSON"`。

---

## 整體決策

- Round 3 後狀態：**ready to push + open PR**
- Round 3 收斂性：0 P1，2 P2 全修，無 deferral。
- 全測試 135 PASSED（**+6 新測試**：4 個 non-shape 4xx + HTML body + array body）。
- 兩個 fix 集中在同一個檔（`embeddings/client.py`），合成單一 commit。

## Process 教訓（記憶已存）

**Second-order bug pattern**：當 review 找到「A 路徑處理錯了」，我修了 A，
但忘記檢查 B 跟 C 是不是踩同樣的概念錯誤。這次：
- Round 2 修「所有非 2xx 都壓 502」→ 把 4xx 拆出來
- Round 3 又指出「4xx 還要再細拆」+「2xx 也要驗 body」

PR #1 round-2 出現過一次（cookie auth → host-prefix 失效）。這次是第二次，
代表這是個 recurring pattern 而不是 one-off：以後改 error handling 路徑時，
要主動 enumerate 所有相鄰路徑（success / non-success、各種 status range）
而不是只修被指出來的那一條。

## 預備 Round 4 的觀察點

預期 codex round 4 應該找不到新東西就收斂；如果還有，可能是：

1. `UpstreamClientError` 的 `code` 是 `"upstream_invalid_request"` —
   OpenAI 標準 `code` 用什麼，要不要對齊。
2. embedding response 還有沒有其他欄位 router 端寫入時可能 KeyError
   （例如 `usage` 不在 dict 裡時 router 不會炸，但 client 看到 None usage 怪）。
3. `_REQUEST_SHAPE_STATUSES` 要不要也涵蓋 408（client request timeout，但
   upstream 觀點下 gateway 才是 client，所以不該算客戶錯）。
