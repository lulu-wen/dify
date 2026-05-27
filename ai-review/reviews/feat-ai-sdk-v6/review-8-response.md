# Review Response: feat/ai-sdk-gateway-pr6 — Round 8 (Codex)

> Response to `reviews/feat-ai-sdk-v6/review-8.md`.

## Summary

| 嚴重度 | 找到 | 已修 | 不修 |
|---|---|---|---|
| [P1] | 0 | — | — |
| [P2] | 1 | 1 | 0 |

唯一 P2 修了。是 R5 reuse-path 的第 3 個 glue 漏洞（R6 / R7 / R8 三輪都根源於 R5）。

## Findings 處理紀錄

---

### Finding 1: [P2] Validate reused dataset keys before accepting them

- **嚴重度**: [P2] correctness
- **影響檔案**: `gateway/src/gateway/admin/registry_merge.py` + `cli.py`
- **動作**: ✅ Fixed
- **未抓到的時機**: R5 設計 reuse path 時，我假設「peer 的 `dataset_api_key` 既然在 registry 裡就是被驗證過的」— 那是錯的，因為：
  1. `GATEWAY_STRICT_STARTUP` 預設 warn-only、L1 失敗的 peer 也能 boot
  2. PR #6 的目的本來就是清理 legacy placeholder（`dataset-not-used-in-pr1`-style）— 這些 peer 一定會有
  3. 我自己的 dry-run sentinel `PLACEHOLDER_DATASET_KEY` 通過 prefix check（故意設計如此），但永遠不是合法 token

#### 驗證

問題鏈：
1. R5 reuse path：peer.dataset_api_key 直接 `return key` if `isinstance(key, str) and key`
2. Peer 有：
   - **placeholder** `dataset-not-used-in-pr1`：startswith("dataset-") ✓、但實際打 Dify 會 401
   - **wrong family** `app-something`：startswith("dataset-") ✗、但前 check 沒擋
   - **sentinel** `dataset-pending-validation-pre-network`：通過 prefix、不是合法 token
3. 新客戶 onboarding 拿這把 bad key 寫進 registry、CLI 報 success、第一次 `/v1/datasets` call 才炸

PR #6 整個 CLI 的目的之一就是清掉 legacy placeholder、結果 reuse path 反過來把它**傳染**給新客戶 — irony 100。

#### 修復內容

**1. 常數搬家**：把 `_PLACEHOLDER_DATASET_KEY` 從 cli.py 提到 registry_merge.py 變成 `PLACEHOLDER_DATASET_KEY`（去底線、export）+ 加 `_DATASET_KEY_PREFIX = "dataset-"`。CLI 從 registry_merge import、單一 source of truth。

**2. `find_shared_workspace_dataset_key` 加兩道 check**：

```python
key = dify.get("dataset_api_key")
if not isinstance(key, str) or not key:
    continue
# Codex review-8 P2:
if not key.startswith(_DATASET_KEY_PREFIX):
    continue  # 連 L1 都不過、不能傳染
if key == PLACEHOLDER_DATASET_KEY:
    continue  # 防 dry-run sentinel 萬一外洩（通過 prefix 但永遠 bad）
return key
```

任一檢查失敗 → `continue`（嘗試下一個 peer）。所有 peer 都 fail → return None → CLI 落到 `_provision_dataset_api_key` path、為新客戶生 fresh key。

**3. 不修 peer 自己的 bad data**：

選擇不在 reuse path 順手「修好」peer。理由：
- CLI 的本意是「加新客戶」、不是「跨客戶 cleanup」
- 自動覆蓋 peer 的 dataset_api_key 等於 silent 修 disk state、operator 無法察覺
- Gateway 的 startup_check（`GATEWAY_STRICT_STARTUP=1`）會 explicitly flag peer 的 L1 fail、那是正確的暴露點

新客戶用 fresh key 不被連累、peer 的 bad state 留給 startup check 抓 — 各司其職。

#### 測試

新 unit tests in `TestFindSharedWorkspaceDatasetKey`：
- `test_peer_key_missing_dataset_prefix_returns_none` — `"wrong-family-bsa_xyz"` peer → None
- `test_peer_key_is_dry_run_sentinel_returns_none` — `PLACEHOLDER_DATASET_KEY` peer → None
- `test_first_valid_peer_wins_when_mixed_with_invalid` — peer 列表混雜時、跳過 bad ones、回第一個 good one

新 e2e tests in `TestSharedModeKeyReuseEndToEnd`：
- `test_peer_with_invalid_dataset_key_falls_through_to_provision`：
  ```python
  # peer 拿 "wrong-family-no-prefix"、跑 add-customer
  assert mock_provision_dataset_key.call_count == 1   # ← 落到 provision
  assert mock_verify_console_credentials.call_count == 0   # ← 沒進 reuse path
  
  loaded = yaml.safe_load(registry_path.read_text())
  new = ... # 新客戶
  assert new["dify"]["dataset_api_key"] == "dataset-mocked-key-12345678"   # ← fresh key
  legacy = ... # 原本 bad peer
  assert legacy["dify"]["dataset_api_key"] == "wrong-family-no-prefix"   # ← 不動
  ```
- `test_peer_with_placeholder_sentinel_falls_through_to_provision` — sentinel case 同理

#### 結果

- 352 tests pass + 3 POSIX-skipped（+5 vs round 7）
- mypy strict 全綠、ruff 全綠

---

## 整體決策

- Round 8 後狀態：**0 outstanding**
- Branch HEAD: 等 commit 後填

## Pattern observation — R6 / R7 / R8 都是 R5 reuse-path 的副作用

| Round | R5 patch 的哪個面向被找出 gap | 抽象 |
|---|---|---|
| 6 | R5 拔了 `_provision_dataset_api_key` 也拔了它 side effect (login 驗 password) | "拔 call 時、列 side effects" |
| 7 | R5 修 deterministic .tmp 名沒 sweep 同 module 其他 site | "fix 一個 site、grep 同 pattern 其他 site" |
| 8 | R5 把 peer 的 key 當「已驗過」、實際沒 explicit 驗 | "trust boundary 上輸入要重驗、不要 transitively trust" |

三條都是「reuse path 設計時 mental model 不夠完整」的不同切面。第三條（這次）的 lesson：

**Trust 不會自動 transitively 傳遞。** Peer 在 registry 裡 ≠ peer 的 data 已被驗證。即使其他組件（startup_check）有驗、那些驗的時機 / 嚴格度 / 預設值都可能不對齊我這條 path 需要的不變式。Reuse path 要自己重驗、不能假設「下游有人驗過」。

往後 PR 我會多問：

> 「我這條 path 用到的 X 是怎麼到我這的？哪一個元件保證它符合我需要的 invariant？那個保證的開關 / 嚴格度 / 預設值是什麼？如果它沒保證、我這條 path 會怎樣？」

存進 [[feedback-validate-inputs-at-trust-boundary]]（要寫的 memory）。

## 下一步

1. **Round 9 驗收斂**（推薦）— 之前每次「我以為會 0」都還有發現、empirically 我的 expectation 偏樂觀
2. R9 = 0 → push + open PR
3. R9 還抓 → 看是 R5 副作用的第 4 條、還是真新軸、繼續修

PR #6 從 R1 到 R8 累計 17 findings（0 P1、13 P2、4 P3）。
