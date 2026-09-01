# 決勝後の後始末 (v2-0) — 実測版 runbook

決勝 (2026-08-19) 用に入れた設定を戻し、提出時から生きているキーを回すための手順。

**結論から: 「戻す」作業は残っていなかった。** 2026-09-01 に live を読み取って確認した。
残っているのは **キーローテ 1 本**と、**株主が決める判断 3 件** (§4)。

| | |
|---|---|
| project / region | `bero-devops-agent` / `asia-northeast1` |
| agent | `https://sida-agent-860561433627.asia-northeast1.run.app` |
| target | `https://sida-target-860561433627.asia-northeast1.run.app` |
| 実行環境 | Windows PowerShell 5.1 想定 (`&&` 不可、1 ブロック 1 コマンド、`curl.exe`) |
| 🔑 | **株主本人のみ**。鍵の値を扱う手順。AI に代行させない |

`gcloud` は PATH に無いことがある。この repo の `scripts/demo.ps1`:28-29 と同じ 2 行で通す。

```
$sdk = Join-Path $env:LOCALAPPDATA 'Google\Cloud SDK\google-cloud-sdk\bin'
```

```
if (Test-Path $sdk) { $env:Path += ";$sdk" }
```

---

## §1 実測した現状 (2026-09-01)

読み取りのみ。**値を画面に出す形式 (`--format=json` での env 全体 / `secrets versions access` /
`scheduler jobs describe`) は使っていない** — それ自体が鍵を画面と gcloud ログに落とすため。

| 項目 | 想定していたこと | 実測 | 判定 |
|---|---|---|---|
| agent の max-instances | 当日 `1` に絞ったまま | `autoscaling.knative.dev/maxScale=20` | **戻す必要なし** |
| target の max-instances | — | `maxScale=20` | 同上 |
| `autosre-demo-rearm` | 当日 pause したまま | **ENABLED**、直近実行 `2026-08-31T16:00:05Z` | **resume の必要なし** |
| `autosre-warm-ping` | — | **PAUSED** | §4-1 の判断 |
| `AUTOSRE_AUTONOMY_ENABLED` | 発表後 OFF のはず | **env var 自体が存在しない** = OFF | 正 |
| agent の live revision | `00036-cx2` | `00036-cx2` (2026-08-05 作成) / traffic 100% | 正 |
| target の live revision | — | `00071-ft4` (2026-08-07 作成) | 正 |
| コンソールキーの門 | — | キー無しの `GET /guard` が **401** | 門は生きている |
| agent `/health` | 200 | **200** | 正 |
| target `/health` | 503 (武装中が正) | **503** | 正 |
| `github-pat` の version | — | **2 (enabled, 2026-07-07 作成)** / 1 は disabled | **ローテ対象** |
| `GITHUB_TOKEN` の参照 | — | `secretKeyRef {name: github-pat, key: latest}` | 反映は revision 更新で起きる |
| GitHub Actions secret | `JUDGING_CONSOLE_KEY` が在る想定 | **secret は 0 件** (`total_count: 0`) | **存在しない。作業不要** |
| repo 内の tfvars / tfstate | — | **無し** | §7 |

### なぜ「戻す作業が要らない」と言い切れるのか

`--max-instances` は**新しい Revision に焼かれる**設定で、変更すると必ず Revision が 1 本増える。
agent の Revision は `00036-cx2` (2026-08-05) が最新、target は `00071-ft4` (2026-08-07) が最新で、
**8/19 付の Revision がどちらにも存在しない**。つまり当日の `--max-instances=1` は
少なくとも Cloud Run のサービスには landed していない。現在値も両方 `20` で、
`terraform/run.tf`:27 の宣言 (`max_instance_count = 20`) と一致している。

Scheduler の pause は Revision を作らないので履歴からは追えないが、**現在 ENABLED で毎時動いている**
(直近 16:00Z)。どちらの経路でも、いま戻すものは無い。

---

## §2 残っている作業 — キーローテ

対象は 2 つ。**どちらも「漏れた証拠」ではなく「露出したまま時間が経っている」ことが理由**。

| 鍵 | 現状 | 効き目 |
|---|---|---|
| GitHub PAT (`github-pat` v2) | 2026-07-07 発行、提出・審査・決勝を通して同じもの | 対象 repo への push / PR merge |
| コンソールキー (`AUTOSRE_CONSOLE_KEY`) | デプロイ以来同じ。決勝当日 `demo.ps1 open` が URL バーに出した状態で投影された | agent run の実行 (Gemini 課金) / PR merge / target 破壊 / **kill switch 解除** |

`README.md`:371 のとおり agent は `allUsers` に `run.invoker` が付いており、**アクセス制御はこのキーだけ**。

### 先に押さえる罠 (ここで事故る)

1. **コンソールキーは 2 か所にある** — サービスの env var と、`autosre-demo-rearm` の URI
   (`terraform/scheduler.tf`:20 が `?key=` で埋めている)。片方だけ回すと毎時の `/reset` が 401 になり、
   **アラートは鳴らない** (uptime check が見ているのは target であってこのジョブではない)。
2. **キーを空にしない** — `server.py`:227-229 が `if not expected: return  # unset -> open` なので、
   空にすると全エンドポイントが無認証で公開される。削除ではなく**差し替え**。
3. **PAT の反映は revision 更新でしか起きない** — secret 由来の env var は**インスタンス起動時**にしか
   解決されない。version を足しただけでは動いているコンテナは旧トークンを持ったまま。
4. **旧キーは変更前に退避する** — 「旧キーが 401 になった」を確かめるのに要る。未定義変数は空に展開され、
   `server.py`:233-238 は空文字列にも 401 を返すので、**退避を忘れると検証が常に合格して見える**。
5. **`gcloud scheduler jobs update http` は既定でパッチ後の Job を標準出力に出す** — つまり入れたばかりの
   鍵入り URI が画面とログに出る。`--format=none` を必ず付ける。
6. **PowerShell のパイプでトークンを渡さない** — UTF-16LE + 改行付与で壊れたトークンが無音で保存され、
   後日 401 になる (`README.md`:445-452 に同じ罠の記録)。`Read-Host` → ASCII ファイル → `--data-file`。
7. **GitHub の `GET /repos/{owner}/{repo}` の `permissions.push` は PAT の権限ではない** —
   認証ユーザーのリポジトリ上の役割を返すので、Contents: Read の PAT でも `true` が返る。**権限の検証に使えない**。

### 手順 (すべて 🔑 = 株主本人)

**順序に意味がある。** PAT を先にやるのは、新旧が同時に生きていられる唯一のローテだから
(GitHub は新トークンを作っても旧を無効化しない)。後で 401 が出たとき、原因がコンソールキー側だと一意に決まる。

**A. 毎時ジョブを一時停止** (キー差し替え中の 401 を避ける。止めても影響は「今回の 1 回、再破壊が飛ぶ」だけ)

```
gcloud scheduler jobs pause autosre-demo-rearm --location asia-northeast1 --project bero-devops-agent
```

**B. 旧コンソールキーを退避** (変更前に。画面共有中はやらない。値は表示しない)

```
$KEY_OLD = ((gcloud run services describe sida-agent --project bero-devops-agent --region asia-northeast1 --format=json | ConvertFrom-Json).spec.template.spec.containers[0].env | Where-Object name -eq 'AUTOSRE_CONSOLE_KEY').value
```

```
if ($KEY_OLD) { "captured: $($KEY_OLD.Length) chars" } else { "EMPTY - stop and check gcloud auth / service name" }
```

**C. 新しい PAT を発行** — `Settings > Developer settings > Personal access tokens > Fine-grained tokens`

| 項目 | 値 |
|---|---|
| Repository access | live の `GITHUB_TARGET_REPO` が指す 1 repo のみ (`terraform/variables.tf`:15 の default は `BERORINPO/sida-target-config`) |
| Contents | Read and write |
| Pull requests | Read and write |
| Issues | Read (`get_user_reviews` が `GET /repos/{repo}/issues` を叩く) |
| Expiration | 90 日 |

発行直後の確認画面で権限を目で見る。fine-grained PAT は後から権限を問い合わせる手段が無い。
**旧 PAT はまだ消さない** (§6 の戻し道)。

**D. PAT を secret に追加** (履歴に残さないため `Read-Host`、パイプは使わない)

```
Set-Content -Path "$env:TEMP\pat.txt" -Value (Read-Host "paste the new PAT") -Encoding ascii -NoNewline
```

```
gcloud secrets versions add github-pat --project bero-devops-agent --data-file="$env:TEMP\pat.txt"
```

```
Remove-Item "$env:TEMP\pat.txt" -Force
```

**E. 新しいコンソールキーを生成** (このウィンドウを閉じるなら値を控える)

```
$KEY_NEW = -join ((1..48) | ForEach-Object { '{0:x}' -f (Get-Random -Maximum 16) })
```

**F. サービスに投入** — この 1 本で新コンソールキーが入り、**同時に新 revision が新 PAT を掴む**

```
gcloud run services update sida-agent --project bero-devops-agent --region asia-northeast1 --update-env-vars AUTOSRE_CONSOLE_KEY=$KEY_NEW --quiet
```

外形的なダウンタイムは無い (新 revision が ready になるまで旧 revision が応答する)。
裏返すと**起動に失敗するとトラフィックが移らず旧キーが生き続ける**ので、次を必ず見る:

```
gcloud run services describe sida-agent --project bero-devops-agent --region asia-northeast1 --format="value(status.latestReadyRevisionName,status.traffic[0].percent)"
```

`00036-cx2` より新しい名前 + `100` が出ること。

**G. Scheduler の URI を更新** (`--format=none` を外さない)

```
gcloud scheduler jobs update http autosre-demo-rearm --location asia-northeast1 --project bero-devops-agent --uri "https://sida-agent-860561433627.asia-northeast1.run.app/reset?key=$KEY_NEW" --format=none
```

`--oidc-service-account-email` を足さないこと。このジョブは意図的に OIDC 無しで `?key=` 認証
(`terraform/scheduler.tf`:7-8)。OIDC を付けて 401 回帰させた実績がある。

更新後、**鍵を出さずに**中身を確認:

```
gcloud scheduler jobs describe autosre-demo-rearm --location asia-northeast1 --project bero-devops-agent --format="value(state,httpTarget.httpMethod,httpTarget.body,httpTarget.oidcToken)"
```

期待: `PAUSED` / `POST` / `e30=` (base64 の `{}`) / oidcToken は空。
`body` が空なら毎時 411 になるので `--message-body "{}"` で入れ直す。

**H. 毎時ジョブを再開**

```
gcloud scheduler jobs resume autosre-demo-rearm --location asia-northeast1 --project bero-devops-agent
```

**I. 検証** → §3 を全部通す

**J. 旧 credential を無効化** (§3 が**全部**通ってから)

```
gcloud secrets versions disable 2 --secret github-pat --project bero-devops-agent
```

旧 PAT の revoke は GitHub の UI から。**即時・取り消し不能**なので、新トークンの稼働を数日見てからでよい。
旧コンソールキーに revoke 操作は無い — F の revision がトラフィックを持った時点で死んでいる。

**K. ローカル痕跡の後始末**

gcloud は `%APPDATA%\gcloud\logs\<日付>\*.log` に**コマンド引数**と**描画された標準出力**を平文で残す。
F と G は新しい鍵を引数に持つので、**このローテの後に**消す。

```
Remove-Item "$env:APPDATA\gcloud\logs" -Recurse -Force
```

次回実行時に作り直されるので壊れるものは無い。

> 2026-09-01 時点のこの PC のログは 22 ファイル (2026-08-05〜06 + 本日)。`AUTOSRE_CONSOLE_KEY` の
> **名前**を含むのは 8 ファイルだが、**値を含む形 (`'value':` の対 / `?key=` / `--update-env-vars`) は 0 件**だった。
> つまり現時点で「この PC のログに本番キーが平文で落ちている」事実は確認できていない。
> それでも K を手順に入れてあるのは、**これからの手順が鍵をそこに書き込むから** —
> B は `--format=json` でサービス全体を取るので env の値 (= 旧キー) が描画出力としてログに落ち、
> F と G は新しい鍵を**コマンド引数**として落とす (引数行は `--format` と無関係に記録される)。
> なお当日オペの記録がこの PC のログに無い (8/19 のファイルが無い) ので、**別 PC で操作していた場合はその PC でも同じことをする**。

---

## §3 検証チェックリスト

- [ ] 新 revision が ready、traffic 100% (F の確認コマンド)
- [ ] 新キーで `GET /guard` が 200

  ```
  curl.exe -s -o NUL -w "%{http_code}\n" -H "Authorization: Bearer $KEY_NEW" "https://sida-agent-860561433627.asia-northeast1.run.app/guard"
  ```

- [ ] **旧キーで `GET /guard` が 401** (B で退避した `$KEY_OLD` を使う。ここが本当のローテ証明)

  ```
  curl.exe -s -o NUL -w "%{http_code}\n" -H "Authorization: Bearer $KEY_OLD" "https://sida-agent-860561433627.asia-northeast1.run.app/guard"
  ```

- [ ] `killswitch.tripped` が `false`、`enabled`/`available` が `true` (`.\scripts\demo.ps1 status` でも可)
- [ ] Scheduler が `ENABLED` に戻っている (G の確認コマンドを再実行)
- [ ] 毎時ジョブが 1 回成功する (次の毎正時のあと `lastAttemptTime` が進み、target が 503 に戻る)

  ```
  gcloud scheduler jobs list --location asia-northeast1 --project bero-devops-agent --format="table(name.basename(),state,lastAttemptTime)"
  ```

- [ ] **新 PAT で実際に書けた** — `POST /reset` はコンテナ内の `GITHUB_TOKEN` で対象 repo の
      `deploy/target-service.env` を書き換える (`recovery.py`:257 `reset_repo_config`)。上の毎時ジョブ成功が
      そのまま書き込み実証になる。手で確かめるなら同じ `/reset` を新キーで叩き、対象 repo の該当ファイルに
      新しい commit が付いたことを見る。**`permissions.push` は検証にならない** (§2 の罠 7)
- [ ] `/trust` が読める (ローテ前と同じ内容であること)

  ```
  curl.exe -s -H "Authorization: Bearer $KEY_NEW" "https://sida-agent-860561433627.asia-northeast1.run.app/trust"
  ```

---

## §4 株主が決めること

1. **`autosre-warm-ping` を PAUSED のままにするか** — 5 分ごとに `/health` を叩いてコールドスタートを
   減らすジョブ。止めていれば無料枠寄り、動かせば初回応答が速い。terraform は「作る」としか書いていないので、
   PAUSED は宣言との差分。**デモをしない期間は止めたままが妥当**だが、判断は株主。
2. **ProtoPedia (作品 8784) に載せた URL に `?key=` が付いているか** — 付いていれば、このキーは
   「露出した」のではなく**最初から公開**されており、ローテは片付けではなく必須。
   付いていた場合、ローテ後はその URL は死ぬ (公開ページの差し替えが要る)。**repo からは確認できない**。
3. ~~**9 月以降の外形監視**~~ — **対応済**。`judging-sweep.yml` の cron が `0 0 * 7,8 *` (7〜8 月だけ)
   で、最後の自動実行が 2026-08-31、次は 2027 年 7 月という状態だった。`liveness-sweep.yml` に改名して
   **通年 (`0 0 * * *`)** に変更済。あわせて「有効なキーで 200 になること」の検査が
   `GET /?key=...` を見ていた (コンソールページはキー無しで配られるので**どんなキーでも 200**) のを、
   キーで守られた `GET /guard` に向け直した。**戻すのは cron 1 行**。

---

## §5 失敗したときの戻し方

| 症状 | 戻し方 |
|---|---|
| F の後、新 revision が ready にならない | トラフィックは旧 revision のまま = 旧キーが有効。`gcloud run services update ... --update-env-vars AUTOSRE_CONSOLE_KEY=$KEY_OLD` で戻す |
| 新 PAT が壊れていた (401 / 書き込めない) | `gcloud secrets versions disable <新>` + `enable 2` で旧 version に戻し、F と同じコマンドで revision を巻き直す。**旧 PAT を revoke する前なら必ず戻せる** |
| キーを空にしてしまった | 全エンドポイントが無認証で公開されている。ただちに F を `$KEY_NEW` で再実行 |
| Scheduler が 401 を出し続ける | G の URI が旧キーのまま。G をやり直す |

---

## §6 ここで意図的にやらないこと

- **`terraform apply` は使わない。** repo に tfvars も tfstate も無く、この PC は terraform の管理主体ではない。
  live には source deploy 由来の実体があり、apply が何を置き換えるか分からない状態で本番に当てない。
  **tfvars がどこかの PC に在る場合は、そこの `console_key` も新しい値に直す** (次の apply が旧キーを黙って戻すため)。
- **kill switch や日次予算のリセット** — 現在 tripped ではないので触らない。必要になったら
  [cost-guard-runbook.md](cost-guard-runbook.md)。
- **コンソールキーの Secret Manager 化** — 平文 env var をやめて `--update-secrets` にすれば、
  値がコマンドラインにも describe の応答にも二度と出ない。**恒久対策として v2 backlog に積む**べきだが、
  ローテと同時にやると失敗時の切り分けができなくなるので分ける。

---

関連: [cost-guard-runbook.md](cost-guard-runbook.md) / [finals-day-checklist.md](finals-day-checklist.md) / [earned-autonomy.md](earned-autonomy.md)
