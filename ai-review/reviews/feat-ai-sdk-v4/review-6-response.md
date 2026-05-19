# Review Response: feat-ai-sdk-v4 — Round 6

> Response to `reviews/feat-ai-sdk-v4/review-6.md`.

## Summary

| 嚴重度 | 找到 | 已修 | 不修 |
|---|---|---|---|
| [P1] | 1 | 1 | 0 |
| [P2] | 2 | 2 | 0 |

PR #4 第六輪 — 找到 3 個 finding。P1 是 round-3 為了 security 而 break 的
PR #3 backward compat，現在 codex 把它從另一個角度（OpenAI SDK extra_body
flow）抓回來。兩個 P2 都是 shared-mode isolation 的細節缺口。

## Findings 處理紀錄

---

### Finding 1: [P1] Preserve multipart upload dataset_id

- **Severity**: [P1]
- **影響檔案**: `gateway/src/gateway/routers/files.py:71-74`
- **動作**: ✅ Fixed（hybrid query + form fallback）

#### 驗證

Round-3 的修法：為了讓 ownership pre-flight 跑在 multipart parse 之前，
我把 `dataset_id` 完全改成 query-only — 連 dedicated mode 也是。

問題：PR #3 R3 既有 contract 是 multipart form 欄位。OpenAI SDK 的
`client.files.create(file=..., extra_body={"dataset_id": ...})` 會把
`extra_body` flatten 進 multipart form，不是 query。所以：
- 既有 PR #3 client → 400 ✗
- OpenAI SDK 慣用 pattern → 400 ✗

而 round-3 的 security 動機（cheap-fail before body parse）只在 **shared mode**
有意義（dedicated 沒有 cross-customer ownership check）。

#### 修復內容

`upload_file` 改成 hybrid：

```python
dataset_id = request.query_params.get("dataset_id")  # 先 query

if strategy.is_shared:
    # Shared mode: query 是 hard requirement
    if not dataset_id:
        raise InvalidRequestError(
            "dataset_id query parameter is required in shared mode "
            "(needed for ownership verification before parsing the body)",
            param="dataset_id"
        )
    await _verify_dataset_ownership_for_files(...)  # cheap-fail OK

# 不論模式：現在 parse multipart body
form = await request.form()

if not dataset_id:
    # Dedicated mode fallback: 從 form 拿 (PR #3 backward compat)
    form_id = form.get("dataset_id")
    if not form_id:
        raise InvalidRequestError("dataset_id required (query or form)", param="dataset_id")
    dataset_id = str(form_id)

# 同理 indexing_technique: 先 query 後 form
...
```

矩陣：

| 客戶送法 | Dedicated mode | Shared mode |
|---|---|---|
| `?dataset_id=X` only | ✓ | ✓ |
| Form `data={"dataset_id":X}` only | ✓ (PR #3 backward compat) | ✗ 400 (shared 需要 query) |
| Both | ✓ (query wins) | ✓ |

Shared mode 仍然 enforce query → cheap-fail before body parse 維持有效。
Dedicated mode form fallback → PR #3 + OpenAI SDK 既有 client 不破。

#### 測試

`TestReview6Fix_FileUploadBackwardCompat`:
- `test_dedicated_upload_with_form_dataset_id_accepted` — PR #3 風格 form data，dedicated mode 仍 200
- `test_dedicated_upload_with_query_dataset_id_accepted` — PR #4 風格 query，也 200
- `test_shared_upload_form_only_dataset_id_rejected` — shared mode + form only → 400，且 message 明說「shared mode requires query」

順手刪掉 `test_upload_with_form_only_dataset_id_is_rejected` (review-3 加的)，
那 test 在斷言 dedicated mode 的 form-only 會被拒——那是現在被認定為 bug 的行為。

---

### Finding 2: [P2] Resolve shared embedding by public model ID

- **Severity**: [P2]
- **影響檔案**: `gateway/src/gateway/routers/datasets.py:109`
- **動作**: ✅ Fixed

#### 驗證

`DatasetCreateRequest.embedding_model` 的 docstring：

> Customer-facing embedding model id (must match an entry in the
> customer's `embedding_models` registry).

也就是「給 `/v1/models` 的 client-facing ID」。但我 shared mode 程式碼：

```python
if requested_id is not None and requested_id != shared.name:
    raise ...
```

直接比 customer-facing ID **vs Dify served name**。這兩個是不同層級的 id。
例子：
- 客戶 `embedding_models`: `id="bge-m3-public", name="bge-m3"`
- Workspace `shared_embedding_model.name = "bge-m3"`
- 客戶送 `embedding_model="bge-m3-public"` → 比對 `"bge-m3-public" != "bge-m3"` → **400 拒絕**

但其實 `"bge-m3-public"` resolve 後 `.name` 就是 `"bge-m3"`，跟 workspace 一致，
應該接受。

#### 修復內容

`resolve_embedding_for_dataset` 在 shared mode 先 `find_embedding_model` resolve，
再比 `.name`：

```python
if requested_id is None:
    return shared.name, shared.provider

entry = customer.find_embedding_model(requested_id)
resolved_name = entry.name if entry is not None else requested_id

if resolved_name != shared.name:
    raise InvalidRequestError(
        f"shared-mode workspace requires embedding_model that resolves to "
        f"'{shared.name}'; received '{requested_id}'"
        + (f" (registered as '{entry.name}')" if entry else " (not in customer registry either)")
        + ". Per-customer embedding_models can still be used directly via "
          "POST /v1/embeddings, but datasets bind to the workspace-global model."
    )
return shared.name, shared.provider
```

訊息更明確：客戶看到 alias、resolved name、workspace 預期 name 三個資訊，
可以馬上知道要怎麼修。

#### 測試

`TestReview6Fix_SharedEmbeddingResolveByCustomerId`：
- `test_shared_create_accepts_customer_facing_embedding_id` — 客戶送
  `bge-m3-public` (alias) → registry resolve 成 `bge-m3` → 跟 workspace
  一致 → 200，且 Dify payload 看到 `embedding_model="bge-m3"`（resolved name）
- `test_shared_create_mismatched_customer_alias_rejected` — alias resolve 成
  別的 name → 400，message 含 alias + resolved name + workspace required name

---

### Finding 3: [P2] Reject duplicate shared customer IDs

- **Severity**: [P2]
- **影響檔案**: `gateway/src/gateway/registry.py:366`
- **動作**: ✅ Fixed

#### 驗證

Shared mode isolation prefix 是 `{customer_id}__`。`_check_dify_consistency`
擋了 mixed mode + 不同 shared_embedding_model，但**沒擋同 customer_id**：

```yaml
customers:
  - sdk_key: bsa_a
    customer_id: tenant-a       # ← 同 customer_id
    dify: {mode: shared, base_url: http://shared.test, ...}
  - sdk_key: bsa_b
    customer_id: tenant-a       # ← 同 customer_id, 不同 SDK key
    dify: {mode: shared, base_url: http://shared.test, ...}
```

→ 兩個 SDK key → 但 prefix 都 `tenant-a__` → SDK A 跟 SDK B 互看資料。
Soft isolation 整個 collapse。

#### 修復內容

`_check_dify_consistency` 加 shared-mode-only 的 customer_id duplicate 檢查：

```python
if next(iter(modes)) == "shared":
    customer_ids = [m.customer_id for m in members]
    duplicates = sorted(
        {cid for cid in customer_ids if customer_ids.count(cid) > 1}
    )
    if duplicates:
        raise ValueError(
            f"shared customers on dify base_url '{base_url}' have duplicate "
            f"customer_ids: {duplicates}. Each shared customer needs a unique "
            f"customer_id because the isolation prefix is derived from it."
        )
```

注意：**dedicated mode 不擋**。Dedicated 沒有 prefix（一個 customer 一個 Dify），
所以同 customer_id 雖然 weird 但不會 collapse isolation。conservative — 不
過度約束 dedicated mode 的 registry 形狀。

#### 測試

`TestReview6Fix_DuplicateCustomerIdInSharedGroup`：
- `test_shared_duplicate_customer_id_same_base_url_rejected` — 同 base_url
  shared mode 同 customer_id 不同 sdk_key → registry 載入 raise
- `test_shared_same_customer_id_different_base_url_allowed` — 兩個獨立 Dify
  各自有 `tenant-a`，不衝突
- `test_dedicated_duplicate_customer_id_same_base_url_allowed` —
  **regression**：dedicated mode 同 customer_id 同 base_url 不擋（保留彈性）

---

## 整體決策

- Round 6 後狀態：**進 round 7 確認真的收斂**（PR #1/2/3 三輪結束，PR #4 第六輪仍有 P1 — 真實 backward compat regression 不是 nitpick）
- 全測試 **258 PASSED**（251 → 258：+8 review-6 tests，-1 過時 review-3 test）
- Ruff clean

## Process 觀察 — round 6 學到的

Round 6 的兩個重點：

1. **Backward compat regression** — round-3 我為了 security pre-flight 把 PR #3
   既有 contract 改了。當時 commit message 也寫了「breaking change」，但**沒
   人 push back**。codex 是過了三輪後才從 OpenAI SDK extra_body flow 的角度
   抓出來。教訓：**「breaking change」commit 是個信號要慎重，不是綠燈**。
   下次發現自己的 fix 需要 break 既有 contract 時，先停下來問：「真的沒有
   不破壞的修法嗎？」這次的 hybrid 就是「不破壞的修法」，但我 round-3 沒
   找到它。

2. **Layered identity confusion** — embedding model 在系統內有兩層 ID：
   - 客戶 facing ID (customer's `embedding_models[i].id`)
   - Dify served name (`shared_embedding_model.name` 或 `EmbeddingModelEntry.name`)
   
   兩層在不同 boundary 有不同意義，我寫 shared mode 邏輯時把兩層比較搞混了。
   下次寫跨多層的對應邏輯時，**先畫一張 layered identity 圖**。

## 預備 Round 7 的觀察點

PR #4 已經 6 輪。看 round 7 是否真收斂。可能會看：

1. P1 fix 的 hybrid 程式邏輯有沒有 edge case（兩個都不給、兩個都給且不同）
2. Shared embedding 比對只比 `name`，但 `provider` 也可能不同 — 是否該驗 `(name, provider)` tuple？
3. Duplicate customer_id 檢查是否該擴展到 sdk_key 相同的情況（已被既有 `from_entries` 早段擋）
