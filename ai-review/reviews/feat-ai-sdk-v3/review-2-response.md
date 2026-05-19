# Review Response: feat-ai-sdk-v3 — Round 2

> Response to `reviews/feat-ai-sdk-v3/review-2.md`.

## Summary

| 嚴重度 | 找到 | 已修 | 不修 |
|---|---|---|---|
| [P1] | 2 | 2 | 0 |
| [P2] | 1 | 1 | 0 |

兩個 P1 是 **新加的 CI 抓到自己** — 我同一個 PR 既加了 `gateway-ci.yml`
（會跑 `ruff check .`），又寫了會被 ruff B008 / B904 拒絕的程式碼。
The CI 是有用的（馬上抓到 lint debt），但 PR 開出去之前 CI 一定紅。

P2 是真實的 Dify 行為陷阱：dataset 創建時 `embedding_model_provider`
沒給的話 Dify 不會報錯，而是**默默 fallback 到 workspace 預設 embedding
模型**。Dataset 用錯模型 → 向量維度不符 → retrieval 一律無命中。

整理：除了 codex 點到的 3 個，順手把 ruff 在整個 gateway tree 找到的
其他 27 個 lint debt 全 `--fix` 清掉了（unused-noqa、unused-import、
quoted-annotation、unsorted-imports、deprecated-import、duplicate test
method、Chinese fullwidth comma），讓 CI 跑進來就 lint clean。

## Findings 處理紀錄

---

### Finding 1: [P1] Replace FastAPI default calls before enabling Ruff CI

- **Severity**: [P1]
- **影響檔案**: `gateway/src/gateway/routers/files.py:44-49`
- **動作**: ✅ Fixed

#### 驗證

`pyproject.toml` 的 ruff config 開了 `select = ["E", "F", "W", "I", "B", "UP", "RUF"]`，
B 系列就包含 B008 (function-call-in-default-argument)。FastAPI 的
慣例寫法 `def f(x: T = File(...))` 直接踩這個規則。

Codex 給的兩種選項：
- (A) 用 PEP 593 `Annotated[T, File(...)]`（推薦，FastAPI 官方支援，
  跟 typing standard 對齊）
- (B) 在 ruff config 加 `extend-immutable-calls` 例外清單

選 (A)，因為：
- 標準的 PEP 593，跟新版 FastAPI 範例一致
- 不需要在 ruff config 維護 FastAPI-specific 例外
- `Annotated[]` 之後加更多 metadata 比較好擴

#### 修復內容

```python
# Before
async def upload_file(
    file: UploadFile = FastapiFile(..., description="..."),
    dataset_id: str = Form(..., description="..."),
    indexing_technique: Literal["..."] = Form(default="high_quality", description="..."),
):

# After (PEP 593)
async def upload_file(
    file: Annotated[UploadFile, FastapiFile(..., description="...")],
    dataset_id: Annotated[str, Form(..., description="...")],
    indexing_technique: Annotated[
        Literal["..."],
        Form(description="..."),
    ] = "high_quality",  # ← default 在 = 後面，不在 Form() 裡（FastAPI 限制）
):
```

整個過程踩了一個小坑：**FastAPI 不接受 `Annotated[..., Form(default=...)]` 內部
寫 default**，要寫在參數 `=` 後面。Pytest 直接抓到，調整完 ruff + pytest 都綠。

---

### Finding 2: [P1] Chain pagination parse errors for Ruff B904

- **Severity**: [P1]
- **影響檔案**: `gateway/src/gateway/routers/datasets.py:332-333` 與 `files.py:215-217`
- **動作**: ✅ Fixed

#### 驗證

兩個 `_int_query` helper 都長這樣：

```python
try:
    value = int(raw)
except ValueError:
    raise InvalidRequestError(f"{name} must be an integer", param=name)
```

B904 要求 in-except re-raise 加 `from exc` 或 `from None`，否則 traceback
會把 ValueError 跟 InvalidRequestError 混在一起，debug 時看不出哪個是
真因。Functional 沒問題但 lint 紅。

#### 修復內容

```python
try:
    value = int(raw)
except ValueError as exc:
    raise InvalidRequestError(f"{name} must be an integer", param=name) from exc
```

兩個 helper 改成一致。`from exc` 比 `from None` 好（保留原因鏈方便偵錯）。

#### 補充清理

順便用 `ruff check . --fix` 清掉 27 個 auto-fixable lint：

- 11 × RUF100 unused-noqa（過時的抑制標記）
- 6 × F401 unused-import
- 4 × UP037 quoted-annotation（`"str"` → `str`，`from __future__ import annotations` 之後不需要）
- 3 × I001 unsorted-imports
- 1 × UP035 deprecated-import
- 1 × F811 duplicate test method name（手動修，rename）
- 1 × RUF001 fullwidth comma（手動加 `# noqa` — 中文標點故意）

CI 第一次跑進來會 lint clean。

---

### Finding 3: [P2] Require provider when binding dataset embeddings

- **Severity**: [P2]
- **影響檔案**: `gateway/src/gateway/routers/datasets.py:117-120`
- **動作**: ✅ Fixed

#### 驗證

之前邏輯：

```python
if embedding.provider is not None:
    payload["embedding_model_provider"] = embedding.provider
```

Provider 是 PR #2 R1 加進 `EmbeddingModelEntry` 的 **optional** 欄位
（為了 backward compat），predicate 「有就帶、沒就不帶」看起來合理。
但 codex 指出 Dify 的真實行為：**dataset create 收不到
`embedding_model_provider`，Dify 不會用 `embedding_model` 名字單獨匹配，
而是直接 fallback 到 workspace default embedding model**。

實際後果：
- 客戶 registry 配了 `embedding_model="bge-m3-fp16"`
- 但忘記填 `provider`
- Gateway 送 Dify 只有 `embedding_model="bge-m3-fp16"`
- Dify 看了 → 沒指定 provider 嘛 → 用 workspace 預設的 `text-embedding-3-small`
- Dataset 建好了，回應正常
- 客戶上傳文件 → 用 OpenAI embedding 向量化（1536 維）
- 客戶後續用 `/v1/embeddings` 直接打 vllm-embed 拿到 bge-m3 向量（1024 維）
  做 retrieval → 維度不符，retrieval 拿不到任何東西
- 客戶以為 KB 有 bug → 開 ticket

這是「silent + 後續才炸」的最糟糕 failure mode。修法：
**選定的 entry 必須有 provider，沒有就 400 拒絕，明確告訴 registry 該補什麼**。

#### 修復內容

`resolve_embedding_for_dataset` 在選定 entry 之後加：

```python
if entry.provider is None:
    raise InvalidRequestError(
        f"embedding model '{entry.id}' is missing the `provider` field; "
        "datasets need both `embedding_model` and `embedding_model_provider` "
        "to bind reliably (Dify silently falls back to the workspace default "
        "otherwise). Set `provider` on the registry entry "
        '(e.g. "langgenius/openai_api_compatible/openai_api_compatible")',
        param="embedding_model",
    )
```

訊息明確告訴運維「在 registry 裡加 provider 欄位」，給範例值。

Router 端的 `if embedding.provider is not None` 移除，直接傳 → 一個地方
保證 invariant。

**Trade-off**：對於只走 `/v1/embeddings` 不建 dataset 的客戶，
`EmbeddingModelEntry` 仍然可以省略 provider。只有 dataset path 強制要。
這保持 PR #2 設計的彈性，同時關掉 dataset path 的 silent failure。

#### 測試

- `test_create_dataset_provider_missing_returns_400` — registry 給一個
  沒有 provider 的 entry，client 用它建 dataset → 400 + `code="invalid_request"`
  + `param="embedding_model"` + message 包含 entry id + "provider"。
- 既有的 `test_create_dataset_with_explicit_embedding_model` 把 assertion
  從「provider 不在 payload」翻轉成「provider 必須在 payload」。
- conftest fixture 跟 `_customer` helper 預設都加上 provider，這樣其他
  17 個 dataset 測試不受影響。

---

## 整體決策

- Round 2 後狀態：**進 round 3 確認收斂**
- Round 2 收斂性：2 P1 + 1 P2 全修，無 deferral
- 全測試 **198 PASSED**（196 → 198：+1 provider missing test + F811 fix 顯出原本
  shadow 掉的 customer entry test）
- Ruff `check .` 也 **all checks passed**，新 CI 第一次跑就會綠
- 三個 fix 分開 commit 方便 reviewer 對應

## Process 教訓（記憶已存）

**「CI 自抓」pattern**：在同一個 PR 既加 CI 又加 code 的時候，**先在本地
跑一次 CI 用的 lint command 才推**。我這次 CI 寫好了卻沒先 `ruff check .`，
就是 codex 用 high-effort reasoning 「猜」我會踩什麼規則才被抓到 — 我自己
應該在本地先驗。

對應未來改善：每次新增 CI workflow 後，把 workflow 內每個 step 都在本地
跑一次再 commit（pyproject.toml 已經有 ruff / mypy 設定，本地能完整重現）。

## 預備 Round 3 的觀察點

預期 codex round 3 可能會看：

1. `Annotated[Form()]` 改寫後 OpenAPI schema 是否還對（doc generation 沒被影響）
2. F811 rename 是否讓兩個 test 都跑（一個對 EmbeddingModelEntry、一個對 CustomerEntry）
3. Provider required 邏輯是否會在 PR #4 shared-Dify mode 衝突
   （PR #4 spec 設計的 `shared_embedding_model` 是 workspace-global，
    要確認跟 PR #3 的 per-customer entry provider 兩個機制不打架）
4. 既有 ruff `--fix` 改了很多檔案 — 沒有跑進 type narrowing 出問題
5. CI workflow 還缺 mypy strict 啟動測試（pyproject 是 strict 但 CI script 沒 fail-fast）
