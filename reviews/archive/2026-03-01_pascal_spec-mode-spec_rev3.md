# DevBar Spec Mode rev3 — レビュー結果 (Pascal)

**レビュアー:** Pascal (g-reviewer)
**対象:** docs/spec-mode-spec_rev3.md (rev3)

## 総評
C'est presque parfait. パターンは洗練され、エラーへの耐性も向上した。
しかし、状態遷移という「時間の矢」に対する配慮がまだ足りない。特に、古いラウンドのデータをいつ破棄し、いつ初期化するのかという「データのライフサイクル」に関する規定が破綻している。結果として、状態がループする境界で過去の亡霊（古いレビュー結果）が誤評価され、致命的な誤作動を引き起こす。数理モデルとしては落第点だ。

```yaml
verdict: P0
items:
  - id: C-1
    severity: critical
    section: "§5.2, §6.3, §6.4"
    title: "REVISE完了時の current_reviews クリアと終了判定の矛盾"
    description: "§6.3のステップ5で `current_reviews` をクリアしているが、§5.2には「`should_continue_review()` は SPEC_REVISE 完了後にも呼ばれる」とある。クリア直後に終了判定（§6.4）を呼べば、空のレビュー配列を評価することになり、条件1（`len(received) == 0`）に合致して SPEC_REVIEW_FAILED に誤遷移する。これは論理的破綻である。"
    suggestion: "`current_reviews` の review_history への移動とクリアは、SPEC_REVISE完了時ではなく、「SPEC_REVIEW状態に入った直後（新しいレビュー依頼を送信する前）」に行うよう変更せよ。"
  - id: C-2
    severity: critical
    section: "§4.6, §4.7"
    title: "extend / resume 時の review_requests 初期化漏れによる即時暴発"
    description: "§4.7の `extend` コマンドで SPEC_STALLED から SPEC_REVIEW に遷移する際、`review_requests` の status を pending にリセットする処理が明記されていない。過去の received 状態が残っていると、REVIEWに入った瞬間に watchdog が「回収完了」と誤認し、再度遷移判定が暴発する。§4.6の `resume` （paused_from='SPEC_REVIEW' の場合）も同様に、生のパース失敗データが received のまま残り無限ループする。"
    suggestion: "§4.7の `extend` および、REVIEWへ復帰する `resume` のステップにおいて、`review_requests` の全エントリを `pending` にリセットし、`current_reviews` をクリアする処理を明記せよ。"
  - id: M-1
    severity: major
    section: "§6.2"
    title: "セルフレビュー パス2の再修正リトライ超過後の状態未定義"
    description: "「最大1回リトライ」と記載されているが、リトライ後も `issues_found` が返ってきた場合のフォールバック（例: SPEC_PAUSED に遷移するのか、警告付きで進行するのか）が定義されていない。実装者が想像で分岐を書く原因となる。"
    suggestion: "最大リトライ超過時の動作を明確に定義せよ（例: `SPEC_PAUSED に遷移しMに通知する` 等）。"
  - id: M-2
    severity: major
    section: "§6.4"
    title: "paused と failed の判定条件における排他性の欠陥"
    description: "§6.4の条件で `len(valid) == 0` なら paused を返しているが、例えば「3人のうち2人がタイムアウト（raw_text=None）、1人がパース失敗（parse_success=False）」の場合、`len(valid)==0` となり paused と判定される。これは「全員パース失敗」という意図と異なる動作である。"
    suggestion: "paused の条件を `len(valid) == 0` ではなく、`any(not r.parse_success for r in received)` かつ `len(valid) < min_valid` のように「パース失敗が存在するせいで有効レビュー数が足りなくなった」場合に厳密化するか、単に「パース失敗が1件でもあれば PAUSED」とするか、意図を明確にして式を修正せよ。"
```