# Review Response: feat/ai-sdk-gateway-pr6 — Round 7 (Codex)

> Response to `reviews/feat-ai-sdk-v6/review-7.md`.

## Summary

| 嚴重度 | 找到 | 已修 | 不修 |
|---|---|---|---|
| [P1] | 0 | — | — |
| [P2] | 1 | 1 | 0 |

唯一 P2 修了。是 round 5 P2 #2 修法的 **sibling site** — 同 module、同 anti-pattern、我沒一起 sweep。

## Findings 處理紀錄

---

### Finding 1: [P2] Avoid deleting a pre-existing probe file

- **嚴重度**: [P2] correctness / unintended filesystem side effect
- **影響檔案**: `gateway/src/gateway/admin/registry_merge.py`
- **動作**: ✅ Fixed
- **未抓到的時機**: Round 5 P2 #2 修 `write_registry_atomic` deterministic tmp filename 時、沒一併 sweep 同 module 的 `check_writable` probe

#### 驗證

`check_writable` 的 probe code：

```python
probe = parent / f".{path.name}.writable-probe"
try:
    probe.touch()                # 既存檔 → 只更新 mtime、不 fail
except OSError as exc:
    raise RegistryMergeError(...) from exc
finally:
    try:
        probe.unlink(missing_ok=True)   # ★ 不分是否我們建的、直接砍
    except OSError:
        pass
```

問題鏈：
1. `Path.touch()` 在檔案已存在時不 fail（POSIX `open(O_WRONLY|O_CREAT)` semantics）→ 只 bump mtime
2. `finally: unlink` 不檢查是不是我們建的 → operator 或其他工具 happen to 在 `.registry.yaml.writable-probe` 放東西 → **第一次跑 `gateway-admin add-customer` 那個檔案就消失**
3. Preflight 階段、registry write 都還沒開始、沒任何 audit trail

跟 round 5 P2 #2 一模一樣：deterministic filename × create-or-reuse × unconditional unlink → 砍到別人的檔。

#### 修復內容

切到 `tempfile.mkstemp`：

```python
try:
    probe_fd, probe_str = tempfile.mkstemp(
        prefix=f".{path.name}.writable-probe.",
        dir=parent,
    )
except OSError as exc:
    raise RegistryMergeError(
        f"registry parent directory {parent} is not writable: {exc}"
    ) from exc

probe = Path(probe_str)
try:
    os.close(probe_fd)
finally:
    try:
        probe.unlink(missing_ok=True)
    except OSError:
        pass
```

順帶簡化：原本 `probe.touch()` 拿來測 writability、現在 mkstemp 本身就在測（mkstemp 成功 = 該 dir 能建檔），不需再 touch 一次。

`mkstemp` 三個保證跟 `write_registry_atomic` 用法一樣：
1. **O_EXCL 底層**：絕不沿用既有檔案
2. **隨機 suffix**：filename 不可預測、`unlink` 砍到的只會是我們建的
3. **0600 atomic**（POSIX）：probe 本身也是 owner-only，雖然 probe 沒寫敏感資料、但 cheap 加分

#### 測試

新 class `TestCheckWritableNoSideEffects` 加 4 個 test：

```python
def test_preexisting_deterministic_probe_name_is_not_deleted(self, tmp_path):
    """放一個 .registry.yaml.writable-probe、跑 check_writable、
    那個檔案必須毫髮無傷。"""
    legacy_probe = tmp_path / ".registry.yaml.writable-probe"
    legacy_probe.write_text("PRE-EXISTING-CONTENT-FROM-OPERATOR\n")
    
    check_writable(registry)
    
    assert legacy_probe.exists()
    assert legacy_probe.read_text() == "PRE-EXISTING-CONTENT-FROM-OPERATOR\n"

def test_no_probe_residue_after_successful_call(self, tmp_path):
    """成功跑完不留任何 probe 檔。"""
    check_writable(registry)
    residue = [p for p in tmp_path.iterdir() if "writable-probe" in p.name]
    assert residue == []

def test_unwritable_parent_still_raises_clean_error(self, tmp_path):
    """parent 是檔案、mkstemp 會 fail、要 wrap 成 RegistryMergeError、
    錯誤訊息對齊。"""
    blockage = tmp_path / "blockage"
    blockage.write_text("not a directory")
    registry = blockage / "registry.yaml"
    
    with pytest.raises(RegistryMergeError, match="cannot create parent directory"):
        check_writable(registry)

def test_preexisting_probe_survives_when_target_already_exists(self, tmp_path):
    """target registry 本來就存在 + probe 也存在 → probe 還是不能被砍。"""
    registry.write_text("customers: []\n")
    legacy_probe = tmp_path / ".registry.yaml.writable-probe"
    legacy_probe.write_text("MARKER-FILE\n")
    
    check_writable(registry)
    
    assert legacy_probe.read_text() == "MARKER-FILE\n"
```

#### 結果

- 347 tests pass + 3 POSIX-skipped（+4 vs round 6）
- mypy strict 全綠、ruff 全綠

#### Sweep — 我有沒有漏其他 site

跑兩個 grep：
```bash
grep -nE '\.tmp|\.probe|\.lock|\.writable-probe' gateway/src/
grep -nE 'Path\(.*\)\s*/\s*[\'"]\.|with_suffix\([\'"]\.[a-z]+[\'"]' gateway/src/
```

結果：除了 `check_writable`（這次修）和 `write_registry_atomic`（round 5 已修）之外，gateway codebase 沒有其他 deterministic 名稱建檔案的 site。PR #6 scope 內 attack surface 收斂。

DifyClient session token / ConsoleSessionPool 都是 in-memory、不寫 disk、不在這條軸的射程內。

---

## 整體決策

- Round 7 後狀態：**0 outstanding**
- Branch HEAD: 等 commit 後填

## Pattern observation — R5 → R7 是「fix 一個 site、忘記 sweep」

R5 P2 #2 修了 `write_registry_atomic`、我那時的 mental model 是「這個 function 有 deterministic .tmp bug」、而不是「**這個 module 有 deterministic-filename pattern**」。Scope 太窄 → 第二個 site (`check_writable`) 帶著同 bug 上線 → R7 才抓到。

**這次學到的工程習慣**（補進 [[feedback_sweep-pattern-on-fix]] 之類的 memory）：

> 修一個 site 時、把 bug 抽象成「pattern」、grep 同 module / 同類型 file、確認其他 site 沒有同 anti-pattern。具體 search query：
> - 修 deterministic-filename 時：grep `with_suffix`, `parent /`, 字串 concat 的 filename
> - 修 timing 問題時：grep call site 的順序、確認所有 caller 都在 validation 之後
> - 修 race condition 時：grep 共享資源的所有 access site

R5 → R7 兩輪 codex 抓的是同個 bug 的兩個 site、加起來其實是「一個」我沒做的 sweep。

## 下一步

1. **Round 8 驗收斂**（建議）— R7 的修法是收斂之手、但 axis 上可能還有 codex 沒走過的角落（DifyClient 內部寫過 disk？probe 外其他 preflight？）。codex 跑一輪 = 0 finding 才算真收斂。
2. R8 = 0 → push + open PR。
3. R8 還抓 → 看是新 axis 還是 sweep 漏的 site、繼續修。

PR #6 從 R1 到 R7 累計 16 findings、跨 7 個 family。Round 4 之後每一輪都是「新 axis」或「同 axis 的延伸」、收斂速度比 PR #5 慢、合理 — PR #6 引入的 attack surface 比 PR #5 多（CLI / filesystem write / Dify credential / shared-mode quota / multi-user host）。
