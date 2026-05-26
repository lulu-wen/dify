# Review Response: feat/ai-sdk-gateway-pr6 — Round 3 (Codex)

> Response to `reviews/feat-ai-sdk-v6/review-3.md`.

## Summary

| 嚴重度 | 找到 | 已修 | 不修 |
|---|---|---|---|
| [P1] | 0 | — | — |
| [P2] | 1 | 1 | 0 |
| [P3] | 1 | 1 | 0 |

兩條都修。Commit `87155adc5`。

## Findings 處理紀錄

---

### Finding 1: [P2] Handle registry write failures after provisioning

- **嚴重度**: [P2]
- **影響檔案**: `gateway/src/gateway/admin/cli.py` + `gateway/src/gateway/admin/registry_merge.py`
- **動作**: ✅ Fixed (two-pronged)

#### 驗證

Round 2 修法把 CustomerEntry + merge_customer 的 validation 提前到 network call 之前。但**沒包含 filesystem-level failures**：`write_registry_atomic` 仍然 raise `OSError`（PermissionError、disk full、parent-is-a-file、etc.）— CLI 只 catch `RegistryMergeError`、OSError 直接 escape 成 traceback、且 Dify key 已建好 → orphan。

#### 修復內容（兩條防線）

**1. 新增 `check_writable(path)` preflight**：

```python
# registry_merge.py
def check_writable(path: Path) -> None:
    """Preflight: probe parent dir + touch a small file."""
    if path.exists() and not path.is_file():
        raise RegistryMergeError(f"...exists but is not a regular file")
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RegistryMergeError(f"cannot create parent {parent}: {exc}") from exc
    probe = parent / f".{path.name}.writable-probe"
    try:
        probe.touch()
    except OSError as exc:
        raise RegistryMergeError(f"parent {parent} not writable: {exc}") from exc
    finally:
        probe.unlink(missing_ok=True)
```

CLI 在 dry-run merge 之後、network call 之前呼叫。catches:
- Path 存在但不是 file（指向 directory）
- Parent 不存在且 `mkdir` 失敗（parent 是檔案）
- Parent 存在但 permission-denied

**2. Post-network catch 加 `OSError` + 印 orphan 警告**：

```python
try:
    new_entry = _build_entry(dataset_api_key)
    existing = load_existing_registry(registry_path)
    merged = merge_customer(existing, new_entry, force=force)
    write_registry_atomic(registry_path, merged)
except (RegistryMergeError, OSError) as exc:
    click.echo(f"ERROR: registry write failed after Dify key provisioning: {exc}", err=True)
    click.echo(
        f"ORPHAN WARNING: a Dify dataset key was created "
        f"({dataset_api_key[:16]}...) but the registry write failed. "
        f"To avoid an orphan credential in Dify, manually revoke this "
        f"key in Dify Web UI → 知識庫 → 服務 API → 管理金鑰. "
        f"Re-run 'gateway-admin add-customer' after fixing the "
        f"filesystem issue.",
        err=True,
    )
    sys.exit(4)
```

Preflight 包 99% 的失敗、OSError catch 是 defense-in-depth for the race (disk full between preflight 跟 write)、且**真的發生時印出 key prefix** 讓 operator 找回 Dify Web UI 撤銷。

#### 測試

`test_unwritable_registry_path_fails_before_network`：
```python
blockage = tmp_path / "blockage"
blockage.write_text("not a directory")
registry_path = blockage / "registry.yaml"  # parent 是檔案、不能 mkdir
result = runner.invoke(cli, [...])
assert result.exit_code != 0
assert "registry path not writable" in result.output
assert "Traceback" not in result.output
assert mock_provision_dataset_key.call_count == 0  # ← key assertion
```

Click 自己的 `dir_okay=False` 抓「path is directory」case；我 test 故意 hit parent-is-a-file case 來驗 `check_writable` 真的 fire。

---

### Finding 2: [P3] Reject non-mapping customer entries cleanly

- **嚴重度**: [P3]
- **影響檔案**: `gateway/src/gateway/admin/registry_merge.py`
- **動作**: ✅ Fixed

#### 驗證

`customers: [null]` 或 `customers: [- "bad string"]` → `_find_customer_index` 對非 dict 呼叫 `.get()` → `AttributeError` → CLI 顯示 traceback。

#### 修復內容

在 `load_existing_registry` 確定 `customers` 是 list 後加 isinstance check：

```python
for i, item in enumerate(raw["customers"]):
    if not isinstance(item, dict):
        raise RegistryMergeError(
            f"registry.yaml customers[{i}] must be a mapping "
            f"(got {type(item).__name__}: {item!r}). "
            f"Fix the file by hand — each customer entry is an object "
            f"with sdk_key / customer_id / dify / models fields."
        )
```

`merge_customer` 之後的 `_find_customer_index` 永遠看到 dict、不會 crash。

#### 測試

`test_registry_with_non_mapping_customer_entry_fails_cleanly`：
```python
registry_path.write_text("customers:\n  - null\n")
result = runner.invoke(cli, [...])
assert result.exit_code != 0
assert "must be a mapping" in result.output
assert "Traceback" not in result.output
assert mock_provision_dataset_key.call_count == 0
```

---

## 整體決策

- Round 3 後狀態：**0 outstanding**
- Branch HEAD: `87155adc5`
- 331 tests pass（+2 regression）、mypy strict + ruff 全綠

## Sub-family 整理

Codex 兩輪抓的 sub-family 在 "**failure after side-effect**" 這個 parent 下：

| Round | Sub-family | 修法 |
|---|---|---|
| 2 | CustomerEntry / registry merge validation 太晚 | Dry-run merge before network |
| 3a | Filesystem write 太晚 + 沒 wrap OSError | Preflight + dual catch + orphan warning |
| 3b | Parser-edge validation 太晚（non-dict entry）| Add isinstance check in load_existing_registry |

每輪 codex 都打到我 generalisation 不夠廣的地方。第 3 輪後我有信心 surface 已經涵蓋大部分 — 剩下 atomic write 的 OS-specific edge cases（Windows os.replace 行為、SIGKILL during write）這類東西 static review 很難看到，要 real Jetson E2E。

## 下一步

1. **Round 4 驗收斂**（user 跑 codex、看是否 0 finding）— 預期收斂、pattern 跟 PR #5 round 4 類似
2. 收斂後 open PR
3. Squash merge

如果 round 4 還抓到 P2，我會 fall back 到「list 所有 OS-call site、claim test coverage 在 mock 層、Jetson E2E 做最終驗收」這個策略。
