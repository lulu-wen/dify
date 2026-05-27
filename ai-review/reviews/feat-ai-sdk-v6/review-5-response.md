# Review Response: feat/ai-sdk-gateway-pr6 — Round 5 (Codex)

> Response to `reviews/feat-ai-sdk-v6/review-5.md`.

## Summary

| 嚴重度 | 找到 | 已修 | 不修 |
|---|---|---|---|
| [P1] | 0 | — | — |
| [P2] | 2 | 2 | 0 |

兩條 P2 都修了。

## Findings 處理紀錄

---

### Finding 1: [P2] Reuse dataset keys for shared workspaces

- **嚴重度**: [P2] correctness / quota footprint
- **影響檔案**: `gateway/src/gateway/admin/cli.py` + `gateway/src/gateway/admin/registry_merge.py`
- **動作**: ✅ Fixed

#### 驗證

Dify `/console/api/datasets/api-keys` cap = **10 keys/workspace**（這是 Dify 編碼進去的硬限制、不是 config）。

Shared mode 在 Dify 那邊**就是「一個 workspace、多 tenant」**：所有 shared-mode 客戶共用 workspace、共用 dataset、共用 embedding model — 邏輯上**完全可以共用同一把 dataset API key**（key 本身是 workspace-scoped、Dify 不做 per-customer 區分）。

但目前 `cli.py:410-415` 對每個 shared 客戶都跑 `_provision_dataset_api_key` → 第 11 個 shared 客戶上線時 **Dify 拒絕**、且失敗發生在 network 之後 → 我們的 round-3 preflight 抓不到（preflight 只看 filesystem、不看 Dify 配額）。

#### 修復內容

**1. 加 `find_shared_workspace_dataset_key()` helper**（registry_merge.py）：

```python
def find_shared_workspace_dataset_key(
    registry_data: dict[str, Any],
    *,
    base_url: str,
    console_email: str,
) -> str | None:
    """Workspace identity = (base_url normalized, console_email)."""
    normalized = base_url.rstrip("/")
    for entry in registry_data.get("customers", []):
        if not isinstance(entry, dict):
            continue
        dify = entry.get("dify")
        if not isinstance(dify, dict):
            continue
        if dify.get("mode") != "shared":
            continue
        if not isinstance(dify.get("base_url"), str):
            continue
        if dify["base_url"].rstrip("/") != normalized:
            continue
        if dify.get("console_email") != console_email:
            continue
        key = dify.get("dataset_api_key")
        if isinstance(key, str) and key:
            return key
    return None
```

Workspace 識別用 `(base_url.rstrip("/"), console_email)` — 跟 `CustomerRegistry._check_dify_consistency` 用一樣的規則（review-3 P2 加的 trailing-slash 正規化）。

**2. CLI 在 network call 之前查表、命中就跳過**：

```python
# 5b. Shared-mode dataset key reuse (codex review-5 P2 #1)
reused_dataset_api_key: str | None = None
if mode == "shared":
    reused_dataset_api_key = find_shared_workspace_dataset_key(
        existing,
        base_url=dify_base_url,
        console_email=dify_admin_email,
    )

if reused_dataset_api_key is not None:
    click.echo(f"Reusing existing workspace dataset key ({...:16}...)...", err=True)
    dataset_api_key = reused_dataset_api_key
else:
    # 原本的 _provision_dataset_api_key path
    ...
```

**3. Orphan warning 邏輯也修了**：

之前 step 7 寫檔失敗時無條件印「請去 Dify Web UI 撤銷 dataset key」。但如果是 reused key、撤銷會炸到其他 shared 客戶 → 加 conditional：

```python
if reused_dataset_api_key is None:
    click.echo("ORPHAN WARNING: ... 請手動撤銷 ...", err=True)
else:
    click.echo("No orphan key to revoke — the dataset key was reused from "
               "an existing shared-mode peer.", err=True)
```

#### 為什麼不選 codex 建議的「allow passing one explicitly」

Codex 提兩種方案：
- (a) auto-reuse from registry
- (b) `--dataset-api-key` flag for explicit pass-in

選 (a) 而不加 (b)，理由：
- (a) 是 zero-config 的、operator 不會忘記做、不依賴文件
- (b) 跟 (a) 同時存在會讓 mental model 變雜（兩條路都能 set key、哪個贏？）
- 真的需要 explicit override 的 case（首次 onboarding 那個 workspace、想用既有 key）很罕見、且當下也可以手 edit registry.yaml

#### 測試

加 6 個 unit + 3 個 e2e（新 class `TestFindSharedWorkspaceDatasetKey` + `TestSharedModeKeyReuseEndToEnd`）：

關鍵 assertion：
```python
def test_second_shared_customer_reuses_peer_key_no_network(...):
    # seed registry 裡有一個 shared-mode customer
    # 跑 add-customer 加第二個 shared 客戶（同 workspace）
    assert mock_provision_dataset_key.call_count == 0   # ← 沒打 network
    assert new["dify"]["dataset_api_key"] == existing_key   # ← 重用了
    assert "Reusing existing workspace dataset key" in result.output
```

覆蓋的 case：
- 命中（同 workspace = 同 base_url + 同 console_email）→ 重用
- 不同 console_email = 不同 workspace → 仍 provision 新 key
- Dedicated mode 永遠 provision、不重用 shared peer
- Trailing slash normalisation（`http://x/` == `http://x`）
- Registry 裡有 malformed entries（None / 字串 / 缺 dify field）→ 跳過、不 crash

---

### Finding 2: [P2] Create the temp registry file exclusively

- **嚴重度**: [P2] security
- **影響檔案**: `gateway/src/gateway/admin/registry_merge.py`
- **動作**: ✅ Fixed

#### 驗證

問題拆解：

1. Round 4 fix 寫：
   ```python
   fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, target_mode)
   ```
2. `O_CREAT` **沒有** `O_EXCL` → 如果 `tmp` 已存在（前一次 SIGKILL 留下的、或被同 host 另一 user 故意放的）、**檔案會被沿用、`mode` 參數被 ignore**。POSIX 規範明文：mode 只在新建檔案時生效。
3. 我們的 `tmp` 命名 = `registry.yaml.tmp`（deterministic、可預測）→ attacker / 殘留 process 都能精準命中
4. 寫完才 chmod 收緊、中間那段（write secrets → chmod）是 window

`tmp` 內容是什麼？`yaml.safe_dump(registry_data, ...)` — 整個 registry，含全部客戶的 `sdk_key` / `dataset_api_key` / `console_password`。Multi-user host 上 0644 = 同機任何 user 都能讀。

#### 修復內容

改用 `tempfile.mkstemp`：

```python
fd, tmp_str = tempfile.mkstemp(
    prefix=f".{path.name}.",
    suffix=".tmp",
    dir=path.parent,
)
tmp = Path(tmp_str)

try:
    file_obj = os.fdopen(fd, "w", encoding="utf-8")
    try:
        yaml.safe_dump(registry_data, file_obj, ...)
    finally:
        file_obj.close()

    # mkstemp 已經給 0600 (POSIX)、只有 target_mode != 0o600 時才 chmod
    # (e.g. 保留原檔 0o400)
    if target_mode is not None and target_mode != 0o600:
        try:
            os.chmod(tmp, target_mode)
        except OSError:
            pass

    os.replace(tmp, path)
except Exception:
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    raise
```

`mkstemp` 保證：
1. **`O_EXCL` 底層**：絕不沿用既有檔案、collision 就失敗
2. **隨機 suffix**：filename 不可預測、attacker 無法事先 place
3. **0600 atomic**：POSIX 上創檔就是 owner-only、沒有「先寫再 chmod」的 window
4. **同 parent dir**：保留 `os.replace` 的原子性（同 fs requirement）

`prefix=f".{path.name}."` 讓 orphan 殘留（process 被 SIGKILL）還是看得出來是這隻 CLI 留的、operator 看 `ls -la` 認得。

#### 測試

新 class `TestExclusiveTempFile` 加 3 個 test：

```python
def test_preexisting_deterministic_tmp_is_not_touched(self, tmp_path):
    """放一個 attacker-controlled registry.yaml.tmp、跑 write、
    確認那個檔案完全沒被動到。"""
    legacy_tmp = path.with_suffix(".yaml.tmp")
    legacy_tmp.write_text("ATTACKER-CONTROLLED...\n")
    
    write_registry_atomic(path, {"customers": []})
    
    assert path.exists()  # 我們的寫成功
    assert legacy_tmp.exists()  # attacker 那個還在
    assert legacy_tmp.read_text() == "ATTACKER-CONTROLLED...\n"  # 沒被動

@pytest.mark.skipif(os.name != "posix", ...)
def test_write_succeeds_at_0600_with_preexisting_permissive_legacy_tmp(...):
    """0644 permissive legacy tmp + umask 022 → 最終 registry 還是 0600。"""
    legacy_tmp.write_text("attacker-placed")
    os.chmod(legacy_tmp, 0o644)
    os.umask(0o022)
    write_registry_atomic(path, {...})
    assert mode_bits & 0o077 == 0   # group + other 無位

def test_atomic_write_cleans_up_random_tmp_on_failure(...):
    """mkstemp 用隨機名、failure 時 except 路徑要清掉。"""
    with patch(..., side_effect=OSError("disk full")):
        write_registry_atomic(...)
    residue = [p for p in tmp_path.iterdir() if ".tmp" in p.name]
    assert residue == []
```

舊的 `test_atomic_write_cleans_up_tmp_on_failure`（測 deterministic `.tmp` 名）改寫成新版本（glob residue）。

---

## 整體決策

- Round 5 後狀態：**0 outstanding**
- 342 tests pass on Windows（+ 3 POSIX-skipped）→ 比 round 4 的 331 多 11 個
- Linux CI 預計 345 pass
- mypy strict 全綠、ruff 全綠

## Pattern observation — round 5 的軸是 round 4 的延伸

| Round | Sub-family within 「post-condition side-effects」 |
|---|---|
| 4 | 我們自己寫的最終檔 (registry.yaml) 的權限 |
| 5a | 我們自己寫的**中間檔** (tmp) 的權限 — 同類但漏掉 |
| 5b | 對**上游 Dify** 留下的痕跡 (workspace 配額) — 同類但跨系統 |

Round 4 的 axis 是 round 1-3「失敗時的 side effect」的對偶（「成功時的 side effect」）。Round 5 把這個 axis 內部繼續展開：
- 範圍從「最終檔」→「過程檔」(5a)
- 範圍從「自家 disk」→「上游 quota」(5b)

兩個都是 round 4 的 mental model 沒延伸到的地方。

## 下一步

1. **Round 6 驗收斂**（codex 還沒抓盡這條軸的可能性 — 例如 Dify Apps quota、process-killed-mid-write 留 .tmp 等等）
2. 收斂後 open GitHub PR

PR #5 在 round 4 收斂、PR #6 至少要到 round 6。理由：每一輪 codex 都還有發現、表示 attack surface 還沒探完。

## Branch HEAD 預期

Commit 將是 round-5 fix（具體 hash 等 commit 後填）。
