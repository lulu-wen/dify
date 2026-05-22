# Review Response: feat-ai-sdk-v3 — Round 1

> Response to `reviews/feat-ai-sdk-v3/review-1.md`.

## Summary

| 嚴重度 | 找到 | 已修 | 不修 |
|---|---|---|---|
| [P1] | 1 | 1 | 0 |
| [P2] | 2 | 2 | 0 |

Three real bugs, all fixed. Pattern repeats again from PR #2: **second-order
bugs** — fixes from earlier PRs (4xx classification, response shape unwrapping)
revealed neighbouring code paths that didn't apply the same lesson. Specifically:

- The `{"document": {...}}` envelope quirk: I'd handled it in
  `_doc_id_from_response` (logging-only) but forgot to apply it to the
  actual response body. Classic «I knew this and still missed it».
- The 4xx passthrough lesson from PR #2 review-3 (embeddings) didn't propagate
  to the brand-new dataset / file methods because I copy-pasted the chat
  client's status check.
- The `agent_thought` cumulative protocol — I assumed Dify sends incremental
  text (like `message` events do) without re-reading the Dify source.

All three are addressed below.

## Findings 處理紀錄

---

### Finding 1: [P1] Unwrap Dify upload responses before returning the file

- **Severity**: [P1]
- **Codex 描述**:
  > When Dify's create-by-file endpoint returns its normal v1.x shape
  > `{"document": {...}, "batch": ...}`, this passes the outer envelope
  > into `_to_file`, so `id` and `name` fall back to empty strings even
  > though the document was created. Clients then cannot reliably poll or
  > delete the uploaded file from the response; unwrap `dify_resp["document"]`
  > here, similar to `_doc_id_from_response`.
- **影響檔案**: `gateway/src/gateway/routers/files.py:86`
- **動作**: ✅ Fixed

#### 驗證

Codex 完全是對的。我之前在 `_doc_id_from_response` 已經處理過這個 wrap 形狀
（用 logging），但 response body 那條路徑忘了套用。後果：

```jsonc
// 客戶看到的 response（修法之前）
{ "id": "", "object": "file", "name": "", "indexing_status": null, ... }
```

`id=""` 直接讓客戶端的 `client.files.delete(file_id)` 失敗（HTTP path 變
`/v1/files/?dataset_id=...`），等於上傳完就「丟」了。

#### 修復內容

1. 把原本只給 logging 用的 `_doc_id_from_response` 升級成
   `_unwrap_document(raw)`，回傳 document payload 本體。
2. `upload_file` 在送進 `_to_file` 之前先 unwrap。
3. Log 用 unwrap 過的 dict 取 `id`，更可靠。

#### 測試

`test_upload_file_unwraps_dify_document_envelope` — `FakeDifyClient` 改回
真實的 wrap shape `{"document": {...}, "batch": "..."}`，斷言客戶端收到
的 `body["id"] == "doc-real-uuid"`、`body["name"] == "manual.pdf"`。

---

### Finding 2: [P2] Preserve Dify client errors for dataset APIs

- **Severity**: [P2]
- **Codex 描述**:
  > For new dataset/file operations, Dify can return expected 4xx
  > responses such as duplicate dataset name, invalid API key, forbidden
  > dataset, or dataset/document not found, but this helper always turns
  > every non-2xx into `DifyUpstreamError` / 502. That makes client
  > mistakes look like gateway outages.
- **影響檔案**: `gateway/src/gateway/dify/client.py:275`
- **動作**: ✅ Fixed（精確分類，跟 PR #2 review-3 結論一致）

#### 驗證

PR #2 review-3 把 `embeddings/client.py` 的 4xx 精確分類成
`_REQUEST_SHAPE_STATUSES = {400, 413, 422}`；其他 4xx（401/403/429）
維持 502 因為那些是 gateway-side credential / rate-limit 問題。

PR #3 新增 dataset / file 方法時複製了 chat client 的 `_raise_for_dify_status`，
**忘了把同樣的 4xx 分類邏輯帶過來**。所以：

- 客戶建重複名 dataset (Dify 409) → client 收到 502 ❌
- 客戶用錯 dataset UUID (Dify 404) → client 收到 502 ❌
- 客戶上傳超大檔 (Dify 413) → client 收到 502 ❌

而 dataset / file 場景下 **404 + 409 是真客戶錯**（embedding 場景下 404
是「上游不認得 model」是 gateway 錯），所以 set 不一樣：

```python
_DATASET_CLIENT_STATUSES = frozenset({400, 404, 409, 413, 422})
```

401/403/429 維持 `DifyUpstreamError` (502)。

#### 修復內容

1. `_raise_for_dify_status` 加 `pass_client_errors: bool = False` 參數
2. 加 module-level constant `_DATASET_CLIENT_STATUSES`
3. 8 個 dataset / document 方法的 status check 改成 `pass_client_errors=True`
4. Chat / Console paths 不動（語義不同）

#### 測試

`test_dify_client.py`：

- `test_dataset_create_4xx_raises_upstream_client_error` — parametrized
  `[400, 404, 409, 413, 422]`，每個 status 都驗 → `UpstreamClientError`，
  且 `exc.status_code` 保留上游原值。
- `test_dataset_create_non_shape_4xx_still_502` — `[401, 403, 429]` 仍是
  `DifyUpstreamError`（gateway-side credential / rate-limit 問題不該誤導
  客戶）。
- `test_dataset_get_404_raises_upstream_client_error` — 取不存在 dataset
  的回應路徑。
- `test_create_document_by_file_413_raises_upstream_client_error` —
  multipart 上傳 413 path。
- `test_chat_blocking_4xx_still_502_unchanged` — **regression**：
  確認 chat path 沒被影響（chat 4xx 仍是 gateway 錯）。

`test_datasets.py`（router-level）：

- `test_dify_409_duplicate_name_passes_through` — 客戶收到 409 +
  `invalid_request_error` envelope + 上游 message 出現在 `error.message`。
- `test_dify_404_on_get_passes_through` — 404 同樣 envelope。

---

### Finding 3: [P2] Emit only new agent thought text as streaming deltas

- **Severity**: [P2]
- **Codex 描述**:
  > Dify's `agent_thought` stream payload contains the persisted full
  > `MessageAgentThought.thought` for that thought id, and subsequent
  > events for the same id are cumulative after appends/tool updates.
  > OpenAI clients concatenate `delta.reasoning_content`, so forwarding
  > the whole `thought` each time duplicates prefixes (for example
  > `"foo"` then `"foobar"` renders as `"foofoobar"`); track the previous
  > thought per event id and emit only the suffix.
- **影響檔案**: `gateway/src/gateway/streaming/converter.py:116-119`
- **動作**: ✅ Fixed

#### 驗證

我寫 R7 時假設 `agent_thought` 跟 `message` 一樣是增量 — **錯**。
Dify source `easy_ui_based_generate_task_pipeline.py:503` 把
`MessageAgentThought.thought` 整顆 dump 進 event，所以 reasoning 模型
邊推理邊更新 thought 時，每個 event 都會夾帶 cumulative text：

```jsonc
// Event 1
{"event":"agent_thought","id":"t1","thought":"User asks RSRP..."}
// Event 2  (NOT incremental)
{"event":"agent_thought","id":"t1","thought":"User asks RSRP... I should explain..."}
```

而 OpenAI SDK 端會把所有 `delta.reasoning_content` 串接：
```python
full_reasoning = "".join(chunk.delta.reasoning_content for chunk in stream)
```
→ 不修的話客戶看到 `"User asks RSRP...User asks RSRP... I should explain..."`，
prefix 重複。

#### 修復內容

`streaming/converter.py`：

1. 加 module-level `last_thought_by_id: dict[str, str]` state（per-stream，
   每次串流呼叫自己一份）。
2. 收到 `agent_thought` event：
   - 取 `id` 與 `thought`
   - 若 `thought == prev` → 跳過（redundant event）
   - 若 `thought.startswith(prev)` → emit `thought[len(prev):]`（純 suffix）
   - 若 prev 不是 prefix（Dify 重寫了內容，例如 tool result 取代舊 draft）
     → emit full new thought（**duplication 比 silent loss 好**）
   - 若沒 `id`（malformed event）→ emit full 每次（fallback）

#### 測試

`test_sse_converter.py`：

- `test_cumulative_thought_emits_only_suffix` — pin 三個 cumulative events
  `["foo", "foobar", "foobar baz"]` → deltas 必須是 `["foo", "bar", " baz"]`。
  也 assert concat 還原成最後的完整 thought（OpenAI client 端 round-trip）。
- `test_redundant_thought_event_skipped` — 同 id 同 thought 重複送 → 第二次
  跳過，不發空 chunk。
- `test_thought_rewrite_falls_back_to_full_emit` — Dify 重寫 thought（前綴
  不一致）→ emit 完整新值，不靜默丟內容。
- `test_thought_without_id_emits_full_each_time` — 沒有 id 的 fallback path。
- 改既有 `test_reasoning_then_message_phases_stream_in_order` 給兩個 event
  不同 `id`，這樣每個都是「首見」走 full-emit 分支（既有測試語義不變）。

---

## 整體決策

- Round 1 後狀態：**進 round 2 確認沒新問題**
- Round 1 收斂性：1 P1 + 2 P2 全修。
- 全測試 196 PASSED（**+18 新測試**：1 P1 unwrap + 5 dataset 4xx parametrize
  + 3 non-shape 4xx + 1 get-404 + 1 doc-413 + 1 chat-regression + 4 cumulative
  thought + 2 router-level dataset 4xx）。
- 三個 fix 分成 3 個 commit（P1 unwrap、P2 4xx、P2 cumulative thought）
  方便 reviewer 各別評估。

## Process 教訓（記憶已存）

**再次的「second-order bug」pattern**：PR #1 round 2、PR #2 round 2-3 都
出現過，PR #3 round 1 又一次。每次都是「修了 A 路徑但忘了相鄰 B/C 路徑」。

具體模式：
- A. PR #2 review-3 修了 embeddings/client.py 的 4xx 分類
- B. PR #3 新增 dataset/file 用同一個 `_raise_for_dify_status` helper
- C. 忘了把 A 的 lesson 套到 B → 變成 round 1 的 P2

未來改善方向：每個 PR 開工前 grep 一下「我可能複製的 helper / pattern」
然後檢查 lesson 是否仍適用。對應的 helper 列表：
- `_raise_for_dify_status` / `_REQUEST_SHAPE_STATUSES` / `_DATASET_CLIENT_STATUSES`
- `_unwrap_document` / `_doc_id_from_response`
- `_to_file` / `_to_dataset` / `_to_segment`

## 預備 Round 2 的觀察點

預期 codex round 2 可能會看：

1. `_unwrap_document` 對非 dict `document` 的 fallback（例如 `document: null`）
   是否安全 → 我用 `isinstance(inner, dict)` 已防護，但可能漏掉 list / str 變形
2. `last_thought_by_id` 沒有 size 限制 — 長串流 (e.g. agent 跑 100 個工具)
   會無限長。是否該加 LRU？目前一輪 stream 內可接受
3. `_DATASET_CLIENT_STATUSES` 沒涵蓋 415 (unsupported media type)，PDF 上傳
   遇到 unknown extension 會回 415，現會被當 gateway error 502
4. 跨 PR 的 helper drift：未來新加路由要怎麼確保套用對的 status 集合
