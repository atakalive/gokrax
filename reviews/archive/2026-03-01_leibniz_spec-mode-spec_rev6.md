# DevBar Spec Mode — 仕様書レビュー（Leibniz, rev6 / やりすぎ版）

対象: `docs/spec-mode-spec_rev6.md` (rev6, 1215行)
commit: rev5=`2755ac2` → rev6=`17066db`

rev5でのP0（dir=0600即死、notify-only適用不可、extend紐づけ不変条件、timeout/parse_failの診断性）は解消されています。rev6時点で致命的な整合破綻は見当たりません。残るのは “設計として危ういが致命ではない” 点（P1〜）です。

---

```yaml
verdict: P1
items:
  - id: M-1
    severity: major
    section: "§3.1（current_reviewsの構造）, §5.3（should_continue_review）"
    title: "current_reviewsが『メタ(reviewed_rev) + reviewer辞書』の混在で、型安全性とパースが壊れやすい"
    description: "`current_reviews` が `reviewed_rev`（メタ情報）と `pascal/dijkstra/...`（reviewerエントリ）を同一dictに混在させている。§5.3では `isinstance(v, dict) and 'status' in v` でフィルタして回避しているが、データモデルが脆い（誤って `reviewed_rev` を dict にした瞬間に混入する、あるいは reviewer名と衝突する）。また JSON schema/型ヒントを書くときにも不利。"
    suggestion: "`current_reviews = { 'reviewed_rev': '2', 'entries': { reviewer: {...} } }` のようにネストしてメタと本体を分離せよ。少なくとも仕様で『予約キー（reviewed_rev等）は reviewer名に使用禁止』を明記する。"

  - id: M-2
    severity: major
    section: "§6.3（last_changes）, §5.1（rev2以降プロンプト）"
    title: "last_changesの出所がimplementer自己申告のみで、diff（last_commit..HEAD）との整合検査が無い"
    description: "rev2以降プロンプトのchangelog_summary等を `last_changes` に依存させるのは合理的だが、last_changesはimplementerのYAML報告に依存する。誤申告/欠落/破損があっても仕様上は検知できず、レビュアーに誤情報を配る危険がある。"
    suggestion: "`added_lines/removed_lines` は git diff numstat を一次ソースに固定し、last_changesはsummaryとして補助に落とす（不一致なら警告/PAUSED）等の検査を仕様に入れよ。"

  - id: M-3
    severity: major
    section: "§5.3（should_continue_review）"
    title: ""received"=パース成功"という定義は強いが、verdict欠落/不正値の扱いが暗黙"
    description: "status導入で診断性は上がったが、`status='received'` の不変条件（必ず verdict/items が正規化済みである等）が仕様で断言されていない。実装で status と parse_success/verdict がズレると判定が壊れる。"
    suggestion: "status の遷移規則（pending→received|timeout|parse_failed）と、received時の必須フィールド（verdict in {APPROVE,P0,P1} 等）を仕様で列挙し、違反時はSPEC_PAUSEDに倒す方針を明記せよ。"

  - id: m-1
    severity: minor
    section: "§4.6（resume）, §4.6の番号"
    title: "resume手順のステップ番号が重複（5が2回）している"
    description: "§4.6で手順番号5が2回出現している。仕様参照（レビュー指摘ID）に番号を使う運用のため、こういう微小な不整合は後で必ず混乱を生む。"
    suggestion: "再採番して一意にせよ。"

  - id: s-1
    severity: suggestion
    section: "§5.5（パース）"
    title: "YAML抽出regex依存は残る（運用でさらに下げられる）"
    description: "『YAMLブロックは1つだけ』は導入済み。とはいえ抽出工程自体は境界条件を残す。"
    suggestion: "レビュアー指示を『返答はYAMLのみ（他テキスト禁止）』にすると抽出不要になり、PAUSED率が下がる。"
```
