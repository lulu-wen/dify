# Review Response: feat/ai-sdk-gateway-pr6 — Round 6 (Codex)

> Response to `reviews/feat-ai-sdk-v6/review-6.md`.

## Summary

| 嚴重度 | 找到 | 已修 | 不修 |
|---|---|---|---|
| [P1] | 0 | — | — |
| [P2] | 1 | 1 | 0 |

唯一 P2 修了。是我 round-5 引入的 regression。

## Findings 處理紀錄

---

### Finding 1: [P2] Verify reused shared workspace credentials

- **嚴重度**: [P2] correctness regression
- **影響檔案**: `gateway/src/gateway/admin/cli.py`
- **動作**: ✅ Fixed
- **引入時機**: Round 5 fix（commit `61bddd118`）

#### 驗證

問題鏈：
1. PR #6 早期版本：CLI 永遠跑 `_provision_dataset_api_key`
   - 它呼 `await client.console_login(...)`（驗 password）
   - 接著 `await client.console_create_dataset_api_key(...)`（建 key）
   - **`console_login` 是 side effect、但它順帶把 operator 的密碼驗了**
2. Round 5 fix：shared-mode reuse 路徑跳過 `_provision_dataset_api_key`
   - 我目的是「不要建第 11 把 key、不要燒配額」
   - 但**跟著也跳掉了 `console_login` 驗證**
3. 後果：operator 打錯 password → registry.yaml 寫了錯的 password → CLI 顯示成功 → runtime 真的要登入 Dify 時（AppManager lazy build）才炸

這是典型「複用一個 function、忽略它的 side effect 是 load-bearing」的失誤。`_provision_dataset_api_key` 名字看起來只做一件事（建 key），實際上做兩件（驗 + 建）。Reuse path 需要其中一件、我給拔掉了兩件。

#### 修復內容

**1. 切出獨立的 `_verify_console_credentials` helper**：

```python
async def _verify_console_credentials(
    *,
    base_url: str,
    console_email: str,
    console_password: str,
) -> None:
    """Login-only credential verification. No dataset-key creation."""
    async with DifyClient(base_url=base_url, timeout_s=30.0) as client:
        await client.console_login(console_email, console_password)
```

跟 `_provision_dataset_api_key` 同個 module、同個錯誤訊息 envelope（CLI handler 已經會 render `DifyUpstreamError`）。

**2. Reuse branch 加 verify call**：

```python
if reused_dataset_api_key is not None:
    click.echo(f"Verifying console credentials against {dify_base_url}...", err=True)
    try:
        asyncio.run(_verify_console_credentials(
            base_url=dify_base_url,
            console_email=dify_admin_email,
            console_password=dify_admin_password,
        ))
    except DifyUpstreamError as exc:
        click.echo(f"ERROR: Dify rejected console credentials: {exc}", err=True)
        click.echo(
            "Shared-mode reuse still requires the supplied console "
            "credentials to be valid for this workspace — they land in "
            "registry.yaml as the truth the runtime uses for lazy App / "
            "Dataset creation. Most likely cause: mistyped or stale "
            "password. Verify against the Dify Web UI login screen, "
            "then re-run.",
            err=True,
        )
        sys.exit(2)
    except Exception as exc:
        click.echo(f"ERROR: could not reach Dify at {dify_base_url}: {exc}", err=True)
        sys.exit(2)

    click.echo(f"Reusing existing workspace dataset key ({...}...)...", err=True)
    dataset_api_key = reused_dataset_api_key
```

順序：**verify pass 才 echo「reusing...」、verify fail 就 exit 2、registry 完全不動**。

#### 為什麼不選 codex 第二方案

Codex 提兩個選項：
- (a) Verify the supplied console login（我選的）
- (b) Copy the already-validated peer credentials

不選 (b) 的理由：
- **隱藏 operator 輸入錯誤**：典型情境 — operator 在 Dify Web UI rotate password、新 customer 用新 password、peer 還沒更新。(b) 會把 peer 的舊 password 寫進新 entry → 新 entry 也壞。(a) 會驗成功（新 password 真的可用）+ peer 在後續操作才炸（peer 自己要 update password 是另一個 bug）。
- **input-as-source-of-truth**：CLI 其他地方都假設 operator 的 CLI args 是 truth。突然在 reuse path silently override 會破壞這個 mental model。
- **Verify 不貴**：一個 console_login HTTP call、不燒任何 Dify 配額（不像 dataset key creation）。網路 cost 可接受。

#### 測試

更新 `test_second_shared_customer_reuses_peer_key_no_network`：

```python
assert mock_verify_console_credentials.call_count == 1, (
    "_verify_console_credentials was not called on the reuse path — "
    "operator's password would land in registry.yaml unvalidated "
    "(codex review-6 P2)."
)
assert "Verifying console credentials" in result.output
```

新測試 `test_reused_key_path_rejects_invalid_console_credentials`：

```python
# Mock verifier 模擬 Dify 回 401
with patch(
    "gateway.admin.cli._verify_console_credentials",
    new=AsyncMock(side_effect=DifyUpstreamError("...401 Unauthorized")),
):
    result = runner.invoke(cli, [..., "--dify-admin-password", "WRONG-TYPO", ...])

assert result.exit_code == 2
assert "Dify rejected console credentials" in result.output
assert "Traceback" not in result.output
assert mock_provision_dataset_key.call_count == 0   # reuse path, still 0
# Critical: 錯的 entry 不會被寫進 registry
loaded = yaml.safe_load(registry_path.read_text())
assert all(c["customer_id"] != "peer-two" for c in loaded["customers"])
```

新加 fixture `mock_verify_console_credentials`（autouse=False、各 test 顯式宣告需要）。

#### 結果

- 343 tests pass + 3 POSIX-skipped（+1 vs round 5）
- mypy strict 全綠、ruff 全綠

---

## 整體決策

- Round 6 後狀態：**0 outstanding**
- Branch HEAD: 等 commit 後填

## Pattern observation — R6 是 R5 的 fix-of-fix

R5 為了解決「workspace 配額」、把整段 network call 拔掉。但 network call 順帶在做的「credential 驗證」是 load-bearing side effect、跟著被拔掉。R6 修法：把那個 side effect 切成獨立的 helper、reuse path 顯式重建它。

教訓：**移除一段 code 時、不只看它的 primary effect、列出 side effects、replace 留下來會用到的。** 跟 Rich Hickey 的 "complecting" 一致 — `_provision_dataset_api_key` 把 verify + create 綁在一起、需要分開時就會出包。

往後 PR 我會多問：「這個 function 的名字描述了它的 effect 嗎、還是只描述了它的 primary effect？」名字若漏掉 side effect、就有複合 bug 的風險。

## 下一步

1. **Round 7 驗收斂**（建議）— R5/R6 是 fix-and-refix loop，跑一輪 codex 看是不是收斂
2. R7 = 0 → push + open GitHub PR
3. R7 還抓 → 繼續修

Codex 的 pattern 是每輪都在 explore 不同 axis、PR #5 收斂於 R4、PR #6 已經 R6、且 R6 是 self-introduced regression、需要再驗一次。
