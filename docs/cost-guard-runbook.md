# コストガード運用手順 (runbook)

エージェントの実行回数を縛る仕組みと、それが**止まってしまったときの戻し方**。
登壇・審査の直前に開くことを想定している。

実装: `packages/agent/src/agents/state_store.py` / 呼び出しは `server.py` の `_claim_run_slot()`。

## 何を縛っているか

状態は GCS 上の 1 個の小さな JSON オブジェクトに置き、generation 前提条件つきの
compare-and-swap で更新する。Cloud Run は `min-instances=0` / `maxScale=20` なので、
プロセス内変数では「1 インスタンスあたり 5 分に 1 回」になってしまい上限として機能しない。

| 環境変数 | 既定 | 効果 |
|---|---|---|
| `AUTOSRE_STATE_URI` | **未設定** | `gs://bucket/object.json`。**未設定だとガード全体が無効** |
| `AUTOSRE_AUTO_COOLDOWN_S` | 300 | 自動検知の連続実行間隔。手動実行には適用しない |
| `AUTOSRE_DAILY_RUN_LIMIT` | 50 | UTC 日ごとの実行上限。超えると**自己停止**する |

`/reset`(毎時の再武装) は実行枠を消費しない。枠を取るのは `/incident` と `/incident/stream`。

## ⚠ 無効でも「成功」に見える

`AUTOSRE_STATE_URI` が未設定のとき、`reserve_run()` は `allowed=True` / `reason="disabled"`
を返す。つまり **Run が通ることはガードが動いている証拠にならない**。無効なガードと
正常なガードは、実行の成否では区別できない。

判別するには状態を直接見る:

```bash
curl.exe -s -H "Authorization: Bearer $KEY" "https://sida-agent-860561433627.asia-northeast1.run.app/guard"
```

**キーは `?key=` ではなくヘッダで渡す。** クエリ文字列はこちら側の Cloud Run
request log に URL ごと記録されるので、キーが自分のログに残る。`?key=` を残してあるのは
ヘッダを送れない EventSource (`/incident/stream`) のためで、手で叩くときに使う理由はない。

読み方:

| 応答 | 意味 |
|---|---|
| `"enabled": true, "available": true` | ✅ 正常に武装している |
| `"enabled": false` | ❌ **ガード無効** (`AUTOSRE_STATE_URI` 未設定) |
| `"enabled": true, "available": false` | ⚠ バケット不在・権限不足で GCS の状態が読めない (この間は実行が素通りする) |
| `"killswitch": {"tripped": true}` | 🛑 停止中。Run を押しても実行されない |

`available: true` は「状態オブジェクトが在る」ではなく「**GCS に到達できた**」の意味。
オブジェクト未作成 (NotFound) は `empty_state()` + `available: true` として現れるので、
`runs_today: 0` / `day: ""` は「まだ 1 度も耐久ガードを通っていない」とも読める。

## 停止スイッチを解除する

日次上限による自己停止は、**人間が解除するまで止まったまま**になる (仕様)。

### 手順 A: エンドポイント経由 (推奨・壇上向け)

```bash
curl.exe -s -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d "{\"tripped\":false}" "https://sida-agent-860561433627.asia-northeast1.run.app/guard/killswitch"
```

応答の `"ok": true` を確認する。`"ok": false` は「そもそもストアが未設定」を意味するので、
解除できたと読んではいけない。

この POST は `reserve_run()` と同じ `_read` → CAS `_write` を通るので、**書き込み権限の
確認にも使える**。Run を 1 回回す ($0.02 + 修正 PR が生成される) 必要はない。
runtime SA に `storage.objects.create` が無いと `reserve_run()` は
`(True, "unavailable")` を返して**黙って素通り**し、日次上限が永久に積み上がらないので、
権限が生きていることは一度は確認しておく (2026-07-30 に確認済)。

### 手順 B: GCS 直接編集 (フォールバック)

エンドポイントが使えないとき。`$URI` は `AUTOSRE_STATE_URI` の値。
今日の実行回数もまとめて初期化されるので、登壇直前のリセットとしても使える。

```bash
printf '{"version":1,"last_run_ts":0,"day":"","runs_today":0,"killswitch":{"tripped":false,"reason":"","ts":0}}' | gsutil cp - $URI
```

## 登壇前チェック (当日)

1. `GET /guard` が `enabled: true` / `available: true` を返す
2. `killswitch.tripped` が `false`
3. `runs_today` に当日の残余裕がある (`AUTOSRE_DAILY_RUN_LIMIT` と比較)
4. リハーサルで枠を使い切った場合は上記**手順 A** でリセットしてから本番に臨む

## 関連する落とし穴

- **Terraform の drift**: 環境変数を `gcloud run services update` で入れた場合、後続の
  `terraform apply` で巻き戻る。Terraform 側もあわせて更新すること
- **`gcloud run deploy --source` は既存の環境変数を保持する**が、保持されたことを
  `GET /guard` で確認するまでは前提にしない
