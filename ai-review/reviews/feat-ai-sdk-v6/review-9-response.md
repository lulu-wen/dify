# Review Response: feat/ai-sdk-gateway-pr6 — Round 9 (Codex)

> Response to `reviews/feat-ai-sdk-v6/review-9.md`.

## Summary

| 嚴重度 | 找到 | 已修 | 不修 |
|---|---|---|---|
| [P1] | 1 | 1 | 0 |
| [P2] | 0 | — | — |

唯一 P1 修了。是 R5 reuse-path 結構性錯誤 — 我用 `(base_url, console_email)` 當 workspace 識別、但 Dify 的 account model 是「一個 email 可以屬多個 workspace」、這個 proxy 不夠。

## Findings 處理紀錄

---

### Finding 1: [P1] Don't identify Dify workspace by email alone

- **嚴重度**: [P1] tenant isolation
- **影響檔案**: `gateway/src/gateway/dify/client.py` + `registry.py` + `admin/cli.py` + `admin/registry_merge.py`
- **動作**: ✅ Fixed
- **失誤時機**: R5 reuse path 設計時、預設 `(base_url, console_email)` 是 unique workspace identity。是 R5 introduced surface 中的第 4 個結構性問題（前 3 個是 R6/R7/R8）。

#### 驗證

問題鏈：
1. Dify 的 `tenant` (workspace) 跟 `account` (email + password) 是 N:M 關係 — 一個 account 可以是多個 tenant 的成員
2. `console_login(email, password)` 回的 session 帶 cookies、但 cookies 裡只有 access_token + csrf_token — **沒有 active tenant_id**
3. Active tenant 是 server side state、由 `POST /console/api/workspaces/current` 才能拿到
4. 我 R5 用 `(base_url, console_email)` 當識別 → 同 admin 在不同 workspace 的 onboarding 會被當成同一個 workspace → key 跨 tenant 傳染

具體 leak scenario：
```
Dify deployment X 裡：
  - admin@op.com 是 Workspace-A 的成員（tenant_id=t1）
  - admin@op.com 也是 Workspace-B 的成員（tenant_id=t2）

Step 1: 在 Workspace-A 加 customer-1 (shared)
  → console_login(admin@op.com)
  → Dify session 落在 t1（current_tenant）
  → 建 dataset-key-t1
  → registry 寫 customer-1.dataset_api_key = dataset-key-t1

Step 2: 在 Workspace-B 加 customer-2 (shared) — 同 base_url、同 email
  → 我 R5 match (base_url, email) → 命中 customer-1
  → reuse customer-1 的 dataset-key-t1
  → registry 寫 customer-2.dataset_api_key = dataset-key-t1

  → customer-2 的所有 dataset 操作 hit t1（Workspace-A）！
  → t2 (customer-2 應該在的 workspace) 看不到、t1 看到混雜的資料
```

Tenant boundary 失守、無錯誤訊息、CLI 顯示成功、operator 沒辦法事後察覺。**這條 silent corruption 為什麼是 P1**。

#### 修復內容

**1. DifyClient 加 `console_get_current_workspace_id`**：

```python
async def console_get_current_workspace_id(self, session: ConsoleSession) -> str:
    """POST /console/api/workspaces/current → returns tenant.id"""
    self._set_session_cookies(session)
    resp = await self._http.post(
        "/console/api/workspaces/current",
        headers=_console_headers(session),
    )
    _raise_for_dify_status(resp)
    data = resp.json()
    # Accept either direct {"id": "..."} or wrapped {"tenant": {"id": "..."}}
    if isinstance(data, dict):
        tenant = data.get("tenant") if isinstance(data.get("tenant"), dict) else data
        if isinstance(tenant, dict):
            tenant_id = tenant.get("id")
            if isinstance(tenant_id, str) and tenant_id:
                return tenant_id
    raise DifyUpstreamError("Dify workspaces/current missing 'id'")
```

Dify 不同小版本回 response shape 不一樣（有時 wrap、有時不 wrap）— 兩種都接。

**2. `DifyConnection` 加 optional `workspace_id`**：

```python
workspace_id: str | None = Field(
    default=None,
    description=(
        "Dify tenant id this customer's session lands in. Captured at "
        "onboarding via POST /console/api/workspaces/current. None on "
        "legacy entries pre-codex-review-9; the shared-mode reuse path "
        "treats None as 'unknown workspace, don't risk cross-tenant reuse'."
    ),
)
```

Optional + default None → 舊 registry 還是 load 得起來、新 onboarding 都會寫進去。

**3. `find_shared_workspace_dataset_key` 簽名換成 `workspace_id`**：

```python
def find_shared_workspace_dataset_key(
    registry_data, *, base_url: str, workspace_id: str,
) -> str | None:
    ...
    peer_workspace_id = dify.get("workspace_id")
    if not isinstance(peer_workspace_id, str) or not peer_workspace_id:
        continue  # legacy entry without workspace_id → skip (safe)
    if peer_workspace_id != workspace_id:
        continue  # different tenant → don't cross-reuse
    ...
```

Legacy entry（沒 workspace_id）→ skip、不冒險 cross-tenant；workspace_id 不 match → skip。

**4. `_provision_dataset_api_key` 回傳 tuple `(workspace_id, dataset_api_key)`**：

順手把 workspace_id 撈回來、registry 寫入時一起存。Dedicated mode 也存（雖然這個 PR 沒用到、未來「runtime tenant 驗證」可能用到）。

**5. CLI flow 重組（shared mode）**：

```python
# 5b. ALWAYS login first for shared mode (codex review-9 P1)
if mode == "shared":
    fetched_workspace_id = asyncio.run(_login_and_fetch_workspace_id(...))
    # ← 這一步也驗了 password（R6 的 _verify_console_credentials 被併進來）
    
    reused_dataset_api_key = find_shared_workspace_dataset_key(
        existing,
        base_url=dify_base_url,
        workspace_id=fetched_workspace_id,  # ← real tenant id, not email
    )

if reused_dataset_api_key is not None:
    dataset_api_key = reused_dataset_api_key
    # workspace_id already in fetched_workspace_id
else:
    # Provision: also returns workspace_id for storage
    fetched_workspace_id, dataset_api_key = asyncio.run(_provision_dataset_api_key(...))

# Build entry with both:
new_entry = _build_entry(dataset_api_key, workspace_id=fetched_workspace_id)
```

成本：reuse path 多一次 HTTP round trip（login + workspaces/current）— 不過還是省了 dataset-key creation（Dify 那邊配額才是真痛）。

#### 為什麼不選「explicit `--workspace-id` flag」方案

選自動 detect 而不要求 operator 手動指定，因為：
- Operator 已經在登入了、active workspace 是 server side state、API 一個 call 就拿到、不勞 operator 知道
- `--workspace-id` 容易出錯（typo、不知道從哪查 uuid）
- 自動 detect 跟 Dify Web UI 行為一致（Web UI 也是登入後看 current workspace、不是先選）

#### 測試

更新 / 新增：

**Unit tests in `TestFindSharedWorkspaceDatasetKey`**：
- 所有舊 test 從 `console_email=` 改用 `workspace_id=`
- registry 資料都加 `workspace_id` field
- 新 test `test_different_workspace_id_is_not_a_match`：同 base_url、同 email、不同 workspace_id → return None
- 新 test `test_peer_without_workspace_id_is_skipped`：legacy peer 沒 workspace_id → skip

**E2E tests in `TestSharedModeKeyReuseEndToEnd`**：
- Fixture `mock_verify_console_credentials` → `mock_login_and_fetch_workspace_id`（回傳 workspace_id string 而不是 None）
- `mock_provision_dataset_key` return value 從 string → tuple `(workspace_id, dataset_api_key)`
- 新 test `test_peer_in_different_workspace_falls_through_to_provision`：
  ```python
  # peer 在 workspace-A
  self._seed_shared_peer(registry_path, workspace_id="workspace-A-tenant")
  
  # 但 operator 這次登入 session 落在 workspace-B
  mock_login_and_fetch_workspace_id.return_value = "workspace-B-tenant"
  mock_provision_dataset_key.return_value = ("workspace-B-tenant", "dataset-fresh-for-B")
  
  # 跑 add-customer 同 email
  result = runner.invoke(cli, [..., "--dify-admin-email", "ws-admin@example.com", ...])
  
  # 結果：fresh provision、不 cross-tenant
  assert mock_provision_dataset_key.call_count == 1
  assert new["dify"]["workspace_id"] == "workspace-B-tenant"
  # Peer 在 A 不動
  assert peer["dify"]["workspace_id"] == "workspace-A-tenant"
  ```
- 新 test `test_peer_without_workspace_id_falls_through_to_provision`：legacy peer e2e

**TestAddCustomerCommand**：
- `test_uppercase_mode_normalised_before_dify_call` 是 shared mode、需要加 `mock_login_and_fetch_workspace_id` fixture

#### 結果

- 355 tests pass + 3 POSIX-skipped（+3 vs round 8）
- mypy strict 全綠、ruff 全綠

---

## 整體決策

- Round 9 後狀態：**0 outstanding**
- Branch HEAD: 等 commit 後填

## Pattern observation — R5 的「workspace identity proxy」是 4 個 bug 的源頭

R5 為了「shared mode 不要燒配額」設計了 reuse path。在那次設計中我用 `(base_url, console_email)` 當 workspace 識別。後續四輪 codex 都根源於那次設計的不完整：

| Round | R5 reuse path 的哪個面向出問題 | 抽象 |
|---|---|---|
| R6 | 拔了 provision 也拔了 verify side effect | "拔 call 時、列 side effects" |
| R7 | sibling 同 module deterministic filename 沒一起 sweep | "fix 一個 site、grep 同 pattern 其他 site" |
| R8 | peer key 沒驗 prefix、bad data 傳染 | "trust 不會 transitively 傳遞" |
| R9 | identity proxy 本身選錯（email ≠ workspace） | "用 proxy 識別 entity 之前、確定 proxy 真的對應 entity** |

R6 / R7 / R8 都是「在現有 identity model 下我有沒有正確處理 X」的問題；**R9 是「identity model 本身錯了」**。Severity 跳到 P1 也合理 — 前三條是 correctness gap、R9 是 isolation 破口。

存進 [[feedback-identity-proxy-must-match-entity]]（要寫的 memory）。

## 下一步

1. **Round 10 驗收斂**（建議、但 empirically 我每次說「應該 0」都不對 — 跑了再說）
2. R10 = 0 → push + open GitHub PR
3. R10 還抓 → 繼續修

PR #6 從 R1 到 R9 累計 18 findings（1 P1、13 P2、4 P3）。

## 補充考量 — 還沒做的延伸

R9 修了 reuse path 的識別、但有兩個延伸沒做（可能 R10 抓 / 可能不抓）：

1. **`_check_dify_consistency` 還是 group by `base_url`**：目前 registry 裡兩個 shared customer 不同 workspace 但同 base_url 會被歸進同一個 consistency 群、強制 `shared_embedding_model` 一致。其實這條 invariant 在「不同 workspace」就不成立（兩個 workspace 各有自己的 embedding plugin）。應該 group by `(base_url, workspace_id)`。但這要 backward-compat 處理 legacy 沒 workspace_id 的 entries — 暫不做、未來再評估。

2. **Runtime tenant 驗證**：gateway runtime 用 `dataset_api_key` 打 Dify、但沒驗 key 對應的 tenant 是不是 entry 宣稱的 workspace_id。理論上 onboarding 寫對了就一致、但有人手 edit registry 還是會破。defense in depth 可加、暫不做。

兩條都不在 R9 修的範圍、列在這裡讓 R10 / 未來 PR 自己決定要不要動。
