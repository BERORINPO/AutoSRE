# 決勝当日チェックリスト (2026-08-19, 渋谷ストリーム 14:20 集合)

指差しで上から順に。**発表 5 分・当日ライブデモあり**の前提。
`$URL` = `https://sida-agent-860561433627.asia-northeast1.run.app`、`$KEY` = コンソールキー。

## まず 1 コマンド (以下の確認をまとめて出す)

```
.\scripts\demo.ps1 status
```

対象サービスの状態・本日の実行回数・停止スイッチ・自律 ON/OFF・信頼台帳を一画面で表示する。
デスクトップの **「AutoSRE 状態確認.cmd」** をダブルクリックでも同じ。
壇上ではこれを見て、下の個別項目は異常時だけ辿る。

- デモ開始 (障害を起こしてコンソールを開く): `.\scripts\demo.ps1` / 「AutoSRE デモ開始.cmd」
- コンソールだけ開く: `.\scripts\demo.ps1 open`

## 朝イチ (自宅 or 移動前)

- [ ] ガードの武装確認 — `enabled: true` / `available: true` であること:

  ```
  curl.exe -s -H "Authorization: Bearer $KEY" "$URL/guard"
  ```

- [ ] `killswitch.tripped` が `false`。リハで自己停止していたら解除:

  ```
  curl.exe -s -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d "{\"tripped\":false}" "$URL/guard/killswitch"
  ```

- [ ] `runs_today` に余裕がある (上限 50。本番+予備で最低 5 枠残す)
- [ ] `/health` が 200、`/target-health` が **503** (= デモが「武装」されている。
      200 のままなら Scheduler の毎時 rearm を待つか `/reset` を叩く)
- [ ] コンソールを開いて 1 回通しで Run → Approve → 復旧 → 返信下書きまで確認
- [ ] **replay 動画が手元で再生できる** (デモ全滅時の命綱。回線非依存の場所に置く)

## 会場到着後 (登壇前)

- [ ] 会場回線でコンソールが開ける (だめならスマホテザリングに切替)
- [ ] `GET /guard` を再確認 (移動中に自動検知が走って枠を食った可能性)
- [ ] `/target-health` が 503 (毎時 rearm が直前に走ったか確認)
- [ ] ブラウザのタブは**コンソール 1 枚だけ** (SSE は複数タブで inCident表示が競合しうる)
- [ ] 画面共有の解像度でタイムラインとコストの 1 行が読めるか目視

## Earned Autonomy の ON/OFF (自律復旧をデモする場合のみ)

普段は **OFF が正** — ON のまま放置すると毎時の rearm と自律復旧が追いかけ合い、
merge PR が 1 日 ~24 本量産される。

- [ ] 登壇前に ON:

  ```
  .\scripts\demo.ps1 autonomy-on
  ```

- [ ] `GET /trust` で対象クラスが `promoted: true` かつ `demoted: false` を確認
      (コンソール上の「承認ゲートの信頼台帳」カードでも可)
- [ ] **発表終了後に必ず OFF**:

  ```
  .\scripts\demo.ps1 autonomy-off
  ```

## 登壇直前 60 秒

- [ ] `/target-health` 503 を最終確認
- [ ] コンソールをリロード (SSE を新しい接続で開始、Last-Event-ID 状態をリセット)
- [ ] タイマー係とアイコンタクト (5 分厳守、Q&A は別)

## 何かおかしい時の分岐

| 症状 | その場の一手 |
|---|---|
| Run 押下で「停止スイッチが入っている」 | 上記 killswitch 解除 POST → リロード → Run |
| Run 押下で「本日の実行予算の上限」 | 同上 (解除 POST は today の枠もリセットしない — 枠は UTC 日次。**手順 B** (runbook) で state を初期化) |
| ストリームが開始しない (認証エラー表示) | URL の `?key=` を確認。だめなら replay 動画へ |
| Gemini 429 / タイムアウト | 1 回だけ再 Run。再発なら replay 動画へ即切替 (粘らない) |
| 会場回線死亡 | テザリング → だめなら replay 動画 |

**判断基準: 2 回失敗したら実演を捨てて動画。** 5 分は戻ってこない。

## 発表後

- [ ] `GET /guard` で当日の消費を記録 (振り返り用)
- [ ] 8/19 以降: コンソールキー / GitHub PAT のローテーション (提出時から公開統計に載っている)
      → 手順は [post-finals-runbook.md](post-finals-runbook.md)。当日オペの巻き戻しは
      2026-09-01 の実測で**すでに不要**と確認済 (max-instances も Scheduler も正常値)

関連: [cost-guard-runbook.md](cost-guard-runbook.md) (ガードの仕組みと解除手順の詳細)
