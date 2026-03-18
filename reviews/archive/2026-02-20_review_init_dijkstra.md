# gokrax 全文レビュー — Dijkstra

**Date:** 2026-02-20
**Reviewer:** Dijkstra
**Scope:** spec.md (v0.6), config.py, gokrax.py, watchdog.py, notify.py
**Verdict:** P1（APPROVE with observations）

---

## 総評

設計思想は正しい。「PJごとに1状態」「冪等watchdog」「LLMトークン消費ゼロのオーケストレーション」——これらは複雑さを管理可能な単位に分解するという原則に忠実だ。状態遷移をホワイトリスト制にし、直接JSON編集を禁止したのも賢明。

コードは全体として**読めるし、追える**。これは褒め言葉だ。プログラムの正しさを論証するには、まず人間がコードを読めなければならない。その条件は満たしている。

以下、改善すべき点を述べる。P0（必須修正）はない。

---

## 設計（spec.md）

### S1. バージョン不整合（軽微）

ファイル冒頭に `Version: 0.6` とあるが、セクション13のタイトルは「2026-02-20追記」。バージョン番号を更新するか、追記日とバージョンの対応を明記すべき。文書の信頼性は細部に宿る。

### S2. DONE状態の二重性

spec §4.3の状態遷移図では `DONE → IDLE` はMのOK後に遷移するように見えるが、watchdog.pyの `process()` 末尾で `state == "DONE"` を検知して自動的に `IDLE` に戻している。つまりMが `DONE` に遷移させた瞬間、次のwatchdog tick（最大1分後）でバッチが消える。

これは意図的な設計か？仕様書には「MがOK → マージ完了 → IDLEに戻る」とあるが、**マージ完了の確認**はどこにも入っていない。watchdogが即座にIDLEにする場合、マージ前にバッチが消えるリスクがある。仕様書を明確にするか、DONE→IDLEの遷移条件を見直すか、どちらか。

### S3. レビュー完了条件「3件以上」の曖昧さ

spec §4.4では「3件以上のレビューコメント」とあるが、これは「3人以上のレビュアーが各Issueにレビューを提出」と読むべきだ。実装は `len(reviews_dict)` で判定しているので人数ベースで正しいが、specの「コメント」という表現が曖昧。

---

## config.py

### C1. 綺麗

言うべきことが少ないのは良いことだ。定数の一元管理、型ヒントこそないが値の意味は明白。パスがハードコードされている点（`/home/ataka/bin/glab`）は環境依存だが、このプロジェクトの運用規模では許容範囲。

### C2. VALID_TRANSITIONSの構造

現在 `dict[str, list[str]]` だが、これは**遷移元→遷移先の許可リスト**として機能している。構造化プログラミングの観点からは問題ない。ただし、仕様変更時に spec と config.py の二重管理になる。単一情報源の原則（Single Source of Truth）を意識するなら、どちらかに寄せることを将来考えるべき。

---

## gokrax.py（CLI）

### D1. flock の使い方が不正確 — 最重要指摘

`load()` と `save()` の flock 実装に問題がある。

```python
def load(path: Path) -> dict:
    with open(path) as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        data = json.load(f)
        fcntl.flock(f, fcntl.LOCK_UN)  # ← ここ
    return data
```

`with open()` のコンテキストマネージャが `f.close()` を呼ぶと、flockは自動的に解放される。だから明示的な `LOCK_UN` は無害だが冗長。問題はそこではない。

**真の問題は `load()` → 処理 → `save()` の間にロックが保持されないこと。**

```python
data = load(path)    # ロック取得→読む→ロック解放
# ↑ この間に watchdog が同じファイルを読み書きできる
data["enabled"] = True
save(path, data)     # ロック取得→書く→ロック解放
```

これはTOCTOU（Time of Check to Time of Use）競合だ。cron watchdogとCLIが同時に動いた場合、一方の書き込みがもう一方に上書きされる。

**対策:** `load_and_lock()` でファイルディスクリプタを保持し、処理完了まで排他ロックを維持する。あるいは、書き込み時にファイルのmtime/hashを検証するoptimistic lockingを入れる。

現実的には1分間隔のcronとCLI手動実行で衝突する確率は極めて低い。だがこれは「確率が低い」であって「起きない」ではない。正しさの論証ができないコードは、正しくない。

ただしP1とする。運用規模を考えれば致命傷にはならない。

### D2. `cmd_revise` の状態判定

```python
if "DESIGN_REVISE" in state:
```

文字列の `in` 演算子は部分一致だ。`state` が `"DESIGN_REVISE"` なら `"DESIGN_REVISE" in "DESIGN_REVISE"` は True だが、もし将来 `"PRE_DESIGN_REVISE"` のような状態が追加されたら誤判定する。`state == "DESIGN_REVISE"` と厳密比較すべき。同じパターンが watchdog.py にも散見される（`"DESIGN" in state` 等）。

### D3. エラーハンドリングの一貫性

`cmd_review` 内の GitLab note 投稿は try/except で握りつぶしているが、pipeline JSON への書き込みは例外をそのまま上げる。方針は統一すべき。GitLab連携が副作用（best-effort）なら、その設計判断をコメントで明記すること。

### D4. argparseの `--verdict` choices

```python
choices=["APPROVE", "P0", "P1", "REJECT"]
```

config.pyの `VALID_STATES` は一元管理されているが、verdict の有効値はここにハードコードされている。config.py に `VALID_VERDICTS` として定義し、ここから参照すべき。

---

## watchdog.py

### W1. 全体構造は良い

`process()` 関数が1つのPJを処理し、`main()` が全PJをイテレートする。関心の分離が適切。early return パターンで条件分岐がフラットに保たれている。構造化プログラミングの教科書に載せてもいいレベル。

### W2. `count_reviews` のエッジケース

```python
min_n = min((len(i.get(key, {})) for i in batch), default=0)
```

バッチが空の場合 `default=0` で `min_n = 0` となり、`MIN_REVIEWS` に達しないため遷移しない。正しい。だが `process()` 冒頭で `if not batch: return` しているので、ここに到達する時点でバッチは非空。`default=0` は防御的プログラミングとして残してよいが、コメントでその意図を明記すべき。

### W3. `clear_reviews` の設計判断

REVISE→REVIEWループ時に過去のレビューをクリアしている。これは「修正後は白紙から再レビュー」という方針で一貫している。良い判断だ。ただし、P1レビューの履歴も消える点はspecに明記すべき。

### W4. load/save の TOCTOU（D1と同根）

watchdog.py にも同じ flock の問題がある。gokrax.py と共通のユーティリティに括り出すべき。現在、ほぼ同一の `load_pipeline` / `save_pipeline` が gokrax.py と watchdog.py に重複している。DRY原則違反。

**共通モジュール（例: `pipeline_io.py`）に抽出** し、両方から import するのが自然。

---

## notify.py

### N1. `get_bot_token` の正規表現によるJSON前処理

```python
text = re.sub(r',\s*([}\]])', r'\1', text)  # trailing comma対策
```

設定ファイルのJSONにtrailing commaが入っている前提のワークアラウンド。動くが美しくはない。設定ファイル側を修正するか、`json5` パーサーを使うか、少なくともコメントで「なぜこれが必要か」を書くべき。

### N2. `requests` の暗黙の依存

`post_discord` 内で `import requests` しているが、これはファイル冒頭の import に含まれていない。関数内 import は遅延ロードとして使えるが、依存の可視性を下げる。`requests` が入っていない環境で `notify_reviewers` を呼ぶと、Discord通知だけ無言で失敗する。依存を冒頭で明示し、なければ起動時にエラーにすべき。

### N3. `send_to_agent` の実装

`openclaw agent` CLIをsubprocessで呼んでいる。OpenClawの `sessions_send` APIを直接使う方が効率的だが、CLIラッパーにすることでPython内部にOpenClaw SDKの依存を持たない判断か。妥当ではあるが、タイムアウトの二重管理（`--timeout` と subprocess の `timeout`）は `timeout + 10` のマジックナンバーが気になる。定数化を。

### N4. `format_review_request` のコマンド生成

レビュアーごとにコピペ用コマンドを生成する設計は実用的で良い。ただし `GOKRAX_CLI` のパスが絶対パスでコマンドに埋め込まれるため、実行環境が変わると壊れる。まあ、現状は単一環境運用だから問題にはならない。

---

## 横断的指摘

### X1. 重複コードの抽出

gokrax.py と watchdog.py に `load` / `save` / `add_history` / `now_iso` が重複している。共通モジュールに抽出すべき。これは怠慢ではなく、MVPの速度優先と理解するが、Phase 2までに解消を。

### X2. テストの不在

テストコードが見当たらない。状態遷移のバリデーション、flock の競合、レビュー完了判定——これらは自動テストで守るべき不変条件だ。「テストはバグの存在を示せるが不在を示せない」とは私の言葉だが、テストがゼロでは存在すら示せない。

### X3. 型ヒントの欠如

Python 3.10+を使っているなら（`dict | None` 構文から推測）、型ヒントをもっと活用すべき。特に `batch` の要素構造が `dict` としか分からない。`TypedDict` か `dataclass` で Issue エントリの型を定義すれば、コードの読解性と保守性が上がる。

---

## 美点（記録に値するもの）

1. **状態遷移のホワイトリスト制** — 不正な遷移をコードレベルで拒否。防御的で正しい。
2. **watchdogの冪等性** — 何度実行しても同じ結果。これはオーケストレーターの最も重要な性質。
3. **LLMトークン消費ゼロの設計判断** — if文で済むものにLLMを使わない。当然のことだが、2026年においてこれを実行できる設計者は少ない。
4. **CLI経由の操作強制** — 直接JSON編集の禁止は、不変条件の維持に不可欠。
5. **仕様書と実装の一致度が高い** — specに書かれた状態遷移が忠実に実装されている。

---

## 指摘サマリー

| ID | 種別 | 対象 | 内容 |
|----|------|------|------|
| S1 | P1 | spec.md | バージョン番号の不整合 |
| S2 | P1 | spec.md | DONE→IDLE の自動遷移とマージ確認の隙間 |
| S3 | P1 | spec.md | 「3件以上のコメント」→「3人以上のレビュー」に表現修正 |
| D1 | P1 | gokrax.py, watchdog.py | flock TOCTOU競合。load→save間にロック不保持 |
| D2 | P1 | gokrax.py | `"DESIGN_REVISE" in state` → `==` 厳密比較に |
| D3 | P1 | gokrax.py | エラーハンドリング方針の不統一 |
| D4 | P1 | gokrax.py | verdict有効値のハードコード → config.pyへ |
| W3 | P1 | watchdog.py / spec.md | REVISE時のレビュークリア仕様を明記 |
| W4 | P1 | gokrax.py, watchdog.py | load/save/add_history の重複 → 共通モジュール化 |
| N1 | P1 | notify.py | trailing comma対策の理由をコメント化 |
| N2 | P1 | notify.py | requests依存の明示化 |
| N3 | P1 | notify.py | タイムアウトのマジックナンバー定数化 |
| X2 | P1 | 全体 | テストコードの不在 |
| X3 | P1 | 全体 | 型ヒント / TypedDict の活用 |

---

**Verdict: P1 — APPROVE**

設計は健全。実装はMVPとして十分な品質。上記のP1指摘はいずれも「今すぐ壊れる」ものではなく「将来の保守性・正確性を高める」ものだ。特にD1（flock TOCTOU）とW4（重複コード）はPhase 2で対処を推奨する。
