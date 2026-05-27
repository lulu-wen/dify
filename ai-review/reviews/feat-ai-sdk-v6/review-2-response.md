# Review Response: feat/ai-sdk-gateway-pr6 — Round 2 (Codex)

> Response to `reviews/feat-ai-sdk-v6/review-2.md`.

## Summary

| 嚴重度 | 找到 | 已修 | 不修 |
|---|---|---|---|
| [P1] | 0 | — | — |
| [P2] | 1 | 1 | 0 |
| [P3] | 2 | 2 | 0 |

全修。Commit `90218d523`。

## Findings 處理紀錄

---

### Finding 1: [P2] Validate registry before creating Dify keys

- **嚴重度**: [P2]
- **影響檔案**: `gateway/src/gateway/admin/cli.py`
- **動作**: ✅ Fixed

#### 驗證

我 self-review P2-1 的 mode-case 修法只動了一條 input path：`mode = mode.lower()` 提前。我**沒有**問「**其他** local-failure 怎麼辦？」。Codex 抓了我漏的一整類：

| Failure mode | 我 fix 後狀態 | 其他 |
|---|---|---|
| `--mode SHARED` lowercase | ✅ 早於 network | — |
| `customer_id` 重複（無 --force）| ❌ 還在 network 之後 | merge_customer 才 raise |
| `customer_id="Bad_Slug"` in shared mode | ❌ 還在 network 之後 | pydantic 才 reject |
| Cross-customer base_url mismatch | ❌ 還在 network 之後 | CustomerRegistry.from_entries 才 reject |

只有 mode-case 早於 network、其他三條都在 network 之後 fail，留 Dify-side orphan dataset key。

#### 修復內容

**Refactor**：把「build CustomerEntry」抽成 closure，trial run 一次（placeholder key）+ real run 一次（real key）：

```python
_PLACEHOLDER_DATASET_KEY = "dataset-pending-validation-pre-network"

def _build_entry(dataset_api_key: str) -> CustomerEntry:
    return CustomerEntry(
        sdk_key=sdk_key,
        customer_id=customer_id,
        dify=DifyConnection(...),
        models=models,
        embedding_models=embedding_models,
    )

# === DRY-RUN ===
try:
    trial_entry = _build_entry(_PLACEHOLDER_DATASET_KEY)
except Exception as exc:
    click.echo(f"ERROR: customer entry validation failed: {exc}", err=True)
    sys.exit(3)

try:
    existing = load_existing_registry(registry_path)
    merge_customer(existing, trial_entry, force=force)
except RegistryMergeError as exc:
    click.echo(f"ERROR: registry merge would fail: {exc}", err=True)
    sys.exit(4)

# === Network call only after local validation passed ===
dataset_api_key = asyncio.run(_provision_dataset_api_key(...))

# === Real write ===
new_entry = _build_entry(dataset_api_key)
existing = load_existing_registry(registry_path)
merged = merge_customer(existing, new_entry, force=force)
write_registry_atomic(registry_path, merged)
```

Placeholder `"dataset-pending-validation-pre-network"` 開頭是 `dataset-`，過 PR #5 的 L1 format check。Trial run 成功才 network。

#### 測試

新增 `TestNoDifyOrphanOnLocalFailure` class、3 個 test：

```python
def test_duplicate_customer_id_fails_before_network(self, ..., mock_provision_dataset_key):
    # seed registry with tenant-a
    # invoke add-customer with same customer_id
    assert result.exit_code != 0
    assert "registry merge would fail" in result.output
    assert mock_provision_dataset_key.call_count == 0  # ← key assertion

def test_bad_slug_in_shared_mode_fails_before_network(self, ..., mock_provision_dataset_key):
    # invoke with --customer-id Tenant_A --mode shared
    assert result.exit_code != 0
    assert mock_provision_dataset_key.call_count == 0  # ← key assertion

def test_malformed_yaml_gives_clean_error_not_traceback(self, ..., mock_provision_dataset_key):
    # write broken yaml to registry.yaml first
    # invoke add-customer
    assert "is not valid YAML" in result.output
    assert "Traceback" not in result.output  # ← clean error
    assert mock_provision_dataset_key.call_count == 0  # ← key assertion
```

每個 test 都斷言 `mock_provision_dataset_key.call_count == 0`。這是 codex 找到的問題的真正 regression guard — 未來如果有人改回「network → validate」，這三個 test 都會 fail。

---

### Finding 2: [P3] Wrap malformed registry reads

- **嚴重度**: [P3]
- **影響檔案**: `gateway/src/gateway/admin/registry_merge.py`
- **動作**: ✅ Fixed

#### 修復內容

```python
try:
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
except yaml.YAMLError as exc:
    raise RegistryMergeError(
        f"registry at {path} is not valid YAML: {exc}. "
        f"Fix the file by hand or move it aside and let "
        f"gateway-admin start fresh."
    ) from exc
except OSError as exc:
    raise RegistryMergeError(
        f"could not read registry at {path}: {exc}"
    ) from exc
```

`yaml.YAMLError` 跟 `OSError` 之前 escape 出 `load_existing_registry`、`add_customer` 的 `except RegistryMergeError` 捕不到、operator 看到 python traceback。現在統一包成 `RegistryMergeError`，CLI handler 正常 catch、印 clean message + exit code 4。

Regression test in #1 above（malformed yaml test）已 cover。

---

### Finding 3: [P3] Correct the embedding-model option help

- **嚴重度**: [P3]
- **影響檔案**: `gateway/src/gateway/admin/cli.py`
- **動作**: ✅ Fixed

#### 驗證

```python
# 修前
help=(
    "Embedding model. Repeatable. Forms: 'id', 'id:endpoint_url', or "
    "'id:endpoint_url:provider'. Optional."
)
```

但 `_parse_embedding_spec`:
```python
if ":" in spec:
    raise click.BadParameter(...)
```

Help **直接騙人**。Operator 跑 `gateway-admin add-customer --help` 看到 `id:endpoint_url` form 跑下去馬上炸 `BadParameter`。

#### 修復內容

```python
help=(
    "Embedding model id. Repeatable. Bare id only (no ':' allowed); "
    "use --embedding-endpoint-url to set the endpoint and the default "
    "OpenAI-compatible provider applies. For non-default provider, "
    "edit registry.yaml after onboarding. Optional."
),
```

`--embedding-endpoint-url` 的 help 也清楚說「one URL shared by all of them in the current invocation」，避免下一個 operator 期待 per-model URL。

我選**改 help 配合 parser** 而不是**改 parser 支援文件的 form**，因為：
- Help 是 contract surface，operator 第一眼看到的
- 那兩個 colon form (`id:endpoint_url`, `id:endpoint_url:provider`) 是我寫 help 時想做的但沒實作的 feature — 不是 parser bug
- 真要 per-model URL 用 `--embedding-model id` + 手編 registry.yaml 已經 cover

---

## 整體決策

- Round 2 後狀態：**0 outstanding**
- Branch HEAD: `90218d523`
- 329 tests pass（+3 regression）、mypy strict + ruff 全綠

## Pattern observation

PR #6 round 2 跟 PR #5 round 2-3 都是 codex 抓「side-effects-before-
validation」family。具體形式不同：

| PR | Round | Codex 角度 |
|---|---|---|
| PR #5 round 2 | DifyClient 包 `httpx.RequestError` 成 `DifyUpstreamError`、`_check_runtime` 的 exception dispatch 把網路掛當 auth 失敗 | wrapping reality vs type contract |
| PR #5 round 3 | lifespan closure 抓 factory、tests 改 `app.state.dify_client_factory` 沒生效 | DI dispatch reality vs intent |
| PR #6 round 2 | local validation 在 network 之後跑、留 Dify orphan | call ordering reality vs intent |

共同點：**Claude 看 control flow 邏輯對就過、Codex 看每一步副作用問「失敗時這條已經寫進去的東西怎麼辦？」**。是兩種不同的閱讀方式。

## 下一步

1. Codex round 3（如果你想驗收斂）
2. 或直接開 PR — 但 PR #5 跑了 4 輪才 converge、PR #6 surface 雖小但通常也需要 round 3 確認 0 finding 才 clean baseline
