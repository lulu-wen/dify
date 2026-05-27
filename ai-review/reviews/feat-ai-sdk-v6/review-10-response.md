# Review Response: feat/ai-sdk-gateway-pr6 — Round 10 (Codex)

> Response to `reviews/feat-ai-sdk-v6/review-10.md`.

## Summary

| 嚴重度 | 找到 | 已修 | 不修 |
|---|---|---|---|
| [P1] | 0 | — | — |
| [P2] | 1 | 1 | 0 |

唯一 P2 修了。是 R8 的 sweep 漏網之魚 + R7 的同類失誤（修一個 instance、沒 grep 其他）。這次的修法走「live 驗證」而不只是擴充黑名單、結構上完整、不留 round 11 的「那其他 placeholder 呢」追問空間。

## Findings 處理紀錄

---

### Finding 1: [P2] Reject legacy dataset placeholders before reuse

- **嚴重度**: [P2] correctness
- **影響檔案**: `gateway/src/gateway/admin/registry_merge.py` + `cli.py` + `startup_check.py`
- **動作**: ✅ Fixed
- **失誤時機**: R8 我加了 placeholder check、但只擋自己的 sentinel `PLACEHOLDER_DATASET_KEY`，沒 grep codebase 找 startup_check.py 裡 documented 的 `dataset-not-used-in-pr1`。我自己的 [[feedback-sweep-pattern-on-fix]] memory 講的就是這個 — 第三次同類失誤（R7 也是 sweep miss）。

#### 驗證

`startup_check.py` line 9 寫的是：
```
dataset_api_key: "dataset-not-used-in-pr1" (or any placeholder)
```

`(or any placeholder)` 這句話是關鍵 — placeholder space 是 **open-ended** 的。純字串黑名單永遠不完整、round 11 可以再丟 `dataset-todo` / `dataset-changeme` 給我。

#### 修復內容 — 兩層、codex 兩個選項都做

**Layer 1 — 便宜 pre-filter（`_KNOWN_DATASET_PLACEHOLDERS`）**：

```python
# registry_merge.py
_KNOWN_DATASET_PLACEHOLDERS: frozenset[str] = frozenset({
    PLACEHOLDER_DATASET_KEY,           # dataset-pending-validation-pre-network
    "dataset-not-used-in-pr1",         # documented legacy placeholder
})

# find_shared_workspace_dataset_key:
if key in _KNOWN_DATASET_PLACEHOLDERS:
    continue
```

擋掉 documented 的 case、不用打網路。但這只是 fast path、不是權威。

**Layer 2 — 權威 live 驗證（`_verify_dataset_api_key`）**：

```python
# cli.py
async def _verify_dataset_api_key(*, base_url, dataset_api_key) -> bool:
    """list 一筆 dataset、跟 L4 startup check 同一套。
    4xx auth reject → False（key 死了）；network error → re-raise（fail fast）。"""
    async with DifyClient(base_url=base_url, timeout_s=30.0) as client:
        try:
            await client.list_datasets(dataset_api_key=dataset_api_key, page=1, limit=1)
        except (DifyUpstreamError, DifyTimeoutError, UpstreamClientError) as exc:
            if is_network_failure(exc):
                raise
            return False
    return True
```

CLI flow 在 `find_shared_workspace_dataset_key` 回 candidate 之後、commit reuse 之前：
```python
if candidate_dataset_api_key is not None:
    key_is_valid = asyncio.run(_verify_dataset_api_key(...))   # network error → exit 2
    if key_is_valid:
        reused_dataset_api_key = candidate_dataset_api_key
    else:
        click.echo("Candidate dataset key rejected by Dify ... provisioning fresh instead")
        # reused 留 None → fall through to provision
```

**Live 驗證才是真正解決 open-ended 問題的關鍵** — 它不在乎 bad string 長什麼樣、只看 Dify 收不收。placeholder / revoked / wrong-workspace token 全都擋。

三種結果：
- Dify 收（200）→ reuse
- Dify 拒（4xx auth）→ 印訊息、fall through 去 provision fresh key
- Network error → exit 2 fail-fast（剛剛 login 成功過、這時 network fail 值得 surface、不要默默 provision 重複 key）

#### Refactor — `is_network_failure` 升 public

Live 驗證要 network-vs-auth 的 exception 判斷（walk `__cause__` 找 `httpx.RequestError` / `DifyTimeoutError` / `OSError`）。`startup_check._is_network_failure` 已經有這套。複製它有 drift 風險（drift 了就重現它當初要修的 network/auth 誤判 bug）、所以升成 public `startup_check.is_network_failure`、cli.py import。沒 test 引用舊私名、blast radius = def + 2 個 internal caller。

#### 測試

**Unit test in `TestFindSharedWorkspaceDatasetKey`**：
- `test_documented_legacy_placeholder_returns_none` — `dataset-not-used-in-pr1` peer → None（pre-filter）

**E2E tests in `TestSharedModeKeyReuseEndToEnd`**：
- `mock_verify_dataset_api_key` fixture（default return True）
- `test_second_shared_customer_reuses_peer_key_no_network` 加 assert `mock_verify_dataset_api_key.call_count == 1`
- `test_reuse_falls_through_to_provision_when_candidate_key_rejected` — verify 回 False → provision 被呼叫、新 entry 拿 fresh key、output 有 "rejected by Dify"
- `test_reuse_fails_fast_when_candidate_verify_network_errors` — verify raise network → exit 2、不 provision、peer-two 不寫入

#### 結果

- 358 tests pass + 3 POSIX-skipped（+3 vs round 9）
- mypy strict 全綠、ruff 全綠

---

## 整體決策

- Round 10 後狀態：**0 outstanding**
- Branch HEAD: 等 commit 後填

## Pattern observation — R10 是 R7 的雙胞胎、但修法升級了

| Round | Sweep miss 的 pattern | 修法 |
|---|---|---|
| R7 | deterministic filename（`.tmp` / probe）| 改 mkstemp、grep 同 module |
| R10 | placeholder string（sentinel / `dataset-not-used-in-pr1`）| 黑名單 + **live 驗證** |

R7 我用「grep 同 pattern sweep」修、結構上還是 enumerable（要列出所有 deterministic 名稱）。R10 我升級成「live 驗證」— **結構上完整、不可 enumerable-incomplete**。差別：

- Enumerable fix（黑名單）：永遠可能漏一個 → 留追問空間
- Structural fix（問 Dify）：不在乎 bad value 長相 → 沒追問空間

這是這 5 輪（R6-R10）我第一次做到「對這條軸結構完整」的修法（R9 identity 也是結構性的：用真 tenant_id 而非 proxy）。

教訓補強 [[feedback-sweep-pattern-on-fix]]：sweep 找 sibling 是必要、但如果那條軸是 open-ended（placeholder、injection、any user input），enumerable fix 本質不夠 — 要找 structural gate（問權威來源、schema validate、type system）。

## 下一步

1. **Round 11 驗收斂** — empirically 我每次預測 0 都錯、所以照跑
2. R11 = 0 → push + open PR
3. R5 reuse path 現在 5 條軸都硬化過（R6 creds / R7 filenames / R8 format / R9 identity / R10 liveness）、其中 R9 R10 是結構性保證、其餘有 exhaustive test

PR #6 從 R1 到 R10 累計 19 findings（1 P1、14 P2、4 P3）。
