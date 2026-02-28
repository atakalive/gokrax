# DevBar Spec Mode — 仕様書レビュー（Leibniz, やりすぎ版）

対象: `docs/spec-mode-spec_rev1.md` (rev1)

以下、指摘は **実装時に詰む/壊れる** ものを優先し、次に仕様の曖昧さ・整合性・運用破綻・擬似コード不整合を列挙します。*Calculemus.*

---

```yaml
verdict: P0
items:
  - id: C-1
    severity: critical
    section: "§3.1, §5.2"
    title: "pipeline.jsonのbatch要素が既存実装と型不整合（issueがstrになる）"
    description: "仕様では spec mode の batch item 例が `\"issue\": \"spec\"` と文字列になっているが、既存 devbar コードは issue を int 前提で扱う（`devbar.py` の argparse で `--issue` は int、`pipeline_io.find_issue` も int 比較、`notify.format_review_request` は Issue本文取得に `glab issue show <int>` を呼ぶ）。このままでは spec mode を既存の batch 機構に載せると各所で例外/誤動作する。"
    suggestion: "spec mode のデータモデルを既存 batch と分離（`spec_batch` を導入）するか、issueを常にintに統一（spec専用は iid=0/負数など予約）し、全関数の型契約を更新せよ。"

  - id: C-2
    severity: critical
    section: "§5.1, §6.1"
    title: "メッセージ送信インターフェースが現実コードと不整合（sessions_send 前提）"
    description: "仕様は `sessions_send` を前提にプロンプトを設計しているが、現行 devbar は `openclaw agent --message`（notify.send_to_agent）と gateway-send（send_to_agent_queued）で送信している。セッションキー/送信制約（改行が消える等）が違い、プロンプト設計・パース成功率・運用が変わる。仕様のまま実装すると『どの送信路を使うか』で破綻する。"
    suggestion: "仕様内で送信路を固定し、その制約（改行保持/最大長/エラー時挙動）を明文化せよ。例: レビュー依頼は send_to_agent（改行保持）で必須、催促のみ queued、など。"

  - id: C-3
    severity: critical
    section: "§2.3, §10.1"
    title: "既存ステートマシンとの“排他”が設計的に未定義（VALID_STATES/TRANSITIONSに未反映）"
    description: "仕様は spec mode を既存実装フローと排他とするが、現行 `config.VALID_STATES/VALID_TRANSITIONS` は DESIGN/IMPLEMENTATION/CODE 系のみで、watchdog.check_transition もそれにハードコードされている。spec_mode: true を見て分岐するという§10擬似コードは現行設計（純粋関数check_transition中心）と整合しない。排他にするなら『どの時点で既存 state を置き換えるのか』『spec_mode をどこで検証するか』が必要。"
    suggestion: "(A) 既存 state 群に SPEC_* を追加し VALID_* と check_transition を拡張、または (B) spec mode 専用 pipeline（別JSON）に分離し watchdog も別エントリにする、いずれかに設計を固定せよ。"

  - id: C-4
    severity: critical
    section: "§6.3（終了判定の擬似コード）"
    title: "should_continue_review擬似コードが未定義変数を参照し、ループ条件が仕様§2.4と齟齬"
    description: "擬似コード `if revise_count >= max_revise_cycles` はローカル変数が存在せず、直前で `config = pipeline[\"spec_config\"]` を作っているのに利用していない。また終了条件の定義が『P1以上なし』と『全レビュアーのverdictにP1/P0が無い』のどちらなのか揺れている。現行 devbar は P0 のみが revise トリガ（P1ではREVISEにならない設計）なので、spec側の“P1以上あり: ループ継続”は大きな方針変更になる。"
    suggestion: "終了判定を『merged_counts の critical/major が0』のように1つに固定し、擬似コードは `config[\"revise_count\"]` 等の実在キーで書け。さらに P1 で revise するなら既存設計との差分（CODE_REVISEの条件）も明記せよ。"

  - id: C-5
    severity: critical
    section: "§3.1"
    title: "spec_configフィールド定義と例が食い違う（skip_review/self_review_passes/queue_file等）"
    description: "§3.1のJSON例では `skip_review/self_review_passes/queue_file` が欠落している一方、後段のテーブルでは存在する。実装時に“どれが必須か”が確定しないと pipeline_io のバリデーションが書けず、後方互換も壊れる。"
    suggestion: "spec_config のスキーマをJSON Schema相当で固定し、必須/任意/デフォルトを列挙し、例はそのスキーマを満たすものに揃えよ。"

  - id: C-6
    severity: critical
    section: "§2.1, §2.2, §2.4"
    title: "状態集合の数え方・終端が不整合（7状態と言いつつIDLEを含む/含まない）"
    description: "§2.2で『7状態: SPEC_REVIEW,...,SPEC_DONE』と書きつつ、遷移図では IDLE が終端として登場し、CLIも start/approve/status を持つ。既存実装の state は常にパイプラインに保存されるので、IDLEを state として扱うなら“8状態+IDLE”などの整理が必要。さらに `--no-issue` 指定時は SPEC_APPROVED で終了とあるが、図では SPEC_APPROVED から SPEC_DONE（またはIDLE）に落ちる分岐が曖昧。"
    suggestion: "(1) state集合を明示（IDLE含むか）、(2) 終端状態は IDLE に統一するか SPEC_DONE を残すか決め、(3) 早期終了（--no-issue/--no-queue/--skip-review）の遷移先を表で固定せよ。"

  - id: C-7
    severity: critical
    section: "§3.2, §9, 現行config.py"
    title: "キューファイル定数が仕様と現行実装で二重定義・名前も不一致"
    description: "仕様は `SPEC_QUEUE_FILE = PIPELINES_DIR / \"devbar-queue.txt\"` とするが、現行 config.py は `QUEUE_FILE = PIPELINES_DIR / \"devbar-queue.txt\"` の直後に `QUEUE_FILE = Path(\"/mnt/s/wsl/work/project/DevBar/devbar-queue.txt\")` で上書きしている（プロジェクト名の大小も違う: DevBar vs devbar）。spec mode が `SPEC_QUEUE_FILE` を導入すると“どれが真の出力先か”が崩壊する。"
    suggestion: "キュー出力先は単一定数（例: `QUEUE_FILE`）に統一し、上書きは環境変数で行う（既にPIPELINES_DIRがそうしている）。spec側は既存名に合わせるか、既存をSPEC_QUEUE_FILEへリネームして全箇所更新せよ。"

  - id: C-8
    severity: critical
    section: "§5.3"
    title: "YAMLパースの『正規表現抽出→フォールバックLLM』が安全性/決定性を欠く"
    description: "レビュー結果の抽出を正規表現に依存すると、コードブロック入れ子・途中欠落・複数yamlブロック等で壊れる。さらに“フォールバックでLLMにパース依頼”は決定性が無く、同一入力で異なる結果（重篤度が変わる）になり得る。devbar はオーケストレータなので、状態遷移条件の非決定化は致命的。"
    suggestion: "フォーマットを厳格化（YAMLは先頭から1ブロックのみ、JSONでも可）、失敗時は『遷移停止+人間介入』に倒すか、LLMフォールバックを使うなら結果を raw と並記し、最終決定は“元テキストのまま”に固定するなどの安全弁を設けよ。"

  - id: C-9
    severity: critical
    section: "§5.4"
    title: "重複統合キー（同一セクション+類似タイトル）が脆弱で誤統合しやすい"
    description: "セクション番号は誤記や重複（本spec内に§6.3重複が既にある）で破綻する。タイトル類似は表記揺れで漏れる/誤統合する。誤統合は“Criticalが消える”最悪ケースを招く。"
    suggestion: "統合キーは (spec_path, rev, section, reviewer, item.id) をまず保持し、統合は『候補提示』に留めるか、embedding類似でも“統合せず関連付け”に留め、Criticalは常に個別保持する規則を入れよ。"

  - id: C-10
    severity: critical
    section: "§10.2"
    title: "タイムアウトが『per reviewer』と『状態全体』で混在し、実装に落ちない"
    description: "§10.2は SPEC_REVIEW/ISSUE_SUGGESTION が per reviewer、他が状態全体としているが、現行 watchdog のタイムアウトは `BLOCK_TIMERS[state]` による“状態全体の経過秒”で、per reviewer を表現するデータ構造が無い。『既存催促ロジック流用』とも整合しない。"
    suggestion: "per reviewer をやるなら pipeline.json に `review_requests: {reviewer: {sent_at, last_nudge_at, timeout_at}}` のような時計を持たせる設計を追加し、状態全体タイムアウトと区別せよ。"

  - id: C-11
    severity: critical
    section: "§12.1"
    title: "レビュー保存パスの同一リポジトリ書き込みは権限/CI/ブランチ運用が未定義"
    description: "specと同じrepoに `reviews/` を保存すると、(1) ブランチはどれか、(2) push権限、(3) 生成物の肥大化、(4) 秘匿情報（レビュー原文）の扱い、が問題になる。現行 devbar は /tmp や pipelines ディレクトリ中心で、repo更新は CC のコミットに依存している。"
    suggestion: "保存先を (A) repo内（バージョン管理する）か (B) pipelines側（運用ログ）か決め、(A)ならブランチ/コミット規約、(B)ならファイルパス規約を仕様化せよ。"

  - id: M-1
    severity: major
    section: "§6.1"
    title: "rev命名規則（rev{N}+1 vs rev{N}{suffix}）が曖昧でdiff計算が壊れる"
    description: "改訂後を `rev{current_rev + 1} (rev{current_rev}{suffix} の場合もあり)` としているが、suffix規則が未定義。`current_rev` が int で、new_rev が "4A" のような str になるなら型が崩れる。revが比較できないと履歴や終了条件が書けない。"
    suggestion: "revは常に string（例: "4", "4A"）に統一し、順序は別フィールド `rev_index:int` で管理する、など機械可読にせよ。"

  - id: M-2
    severity: major
    section: "§4.2"
    title: "CLIのオプションとpipeline.jsonのフィールドが対応していない"
    description: "`devbar spec start` は `--model` を取るが、どこに保存するか（pipelineのどのキーか）が未定義。現行実装は `cc_plan_model/cc_impl_model` のように pipeline に保存している。spec mode でも同様の保存場所が必要。"
    suggestion: "CLI→pipelineへの写像表（フラグ名→spec_configキー）を仕様に追加し、devbar.pyで一意に書けるようにせよ。"

  - id: M-3
    severity: major
    section: "§5.1"
    title: "rev2以降のdiff情報（added_lines/removed_lines/changelog_summary）の生成方法が未定義"
    description: "added/removed をどのdiffで計算するか（前revとのgit diffか、ファイル差分か）、changelog_summaryは誰が書くか（LLM? implementer?）が決まっていない。ここが曖昧だと“レビュー依頼の入力”が組めず、レビュー品質もぶれる。"
    suggestion: "前revのコミットハッシュを必ず持ち、`git diff --numstat <old>..<new> -- <spec_path>` で計算、summaryは実装者のYAML報告 `changes:` を一次ソースにする、等の規約を定めよ。"

  - id: M-4
    severity: major
    section: "§5.3"
    title: "SpecReviewItem.idの命名規則が未定義で衝突する"
    description: "例では C-1/M-1/m-1 等が混在するが、(1) 大文字小文字で区別するのか、(2) reviewerごとにローカルなのか、(3) merged後も同一idを保つのか、が未定義。衝突すると reflected_items の追跡が不可能になる。"
    suggestion: "idはレビュアーごとにローカルなら `reviewer_id + ':' + local_id` を正規化IDにし、mergedでは `merged_id` を別に付与せよ。"

  - id: M-5
    severity: major
    section: "§2.4, §4.3"
    title: "手動approveの権限・監査・安全性が未定義"
    description: "`devbar spec approve` が誰でも実行できるとP0を握り潰せる。現行devbarはDiscordコマンドやCLIをローカルで叩く前提だが、spec mode でも“誰がいつ強制終了したか”は history に残すべき。"
    suggestion: "actor を必須にして history に `actor: M` 等を必ず記録、また approve 実行時にDiscordへ監査ログ投稿する等を仕様化せよ。"

  - id: M-6
    severity: major
    section: "§10.1"
    title: "check_spec_mode擬似コードが遷移関数transition_to前提だが、現行はcheck_transition中心"
    description: "仕様では `transition_to(project, "ISSUE_SUGGESTION")` のような副作用関数が登場するが、現行 watchdog は `check_transition(state,batch,data)->TransitionAction` を純粋関数として設計している。設計哲学が違うため、実装の差分が大きく、テストも書けない。"
    suggestion: "spec mode も `check_transition_spec(...) -> TransitionAction` の純粋関数に落とし込み、既存と同じ“DCL+lock内再計算”パターンに合わせよ。"

  - id: m-1
    severity: minor
    section: "§6.3"
    title: "§6.3が重複しており、参照が壊れる"
    description: "本文でも注意書きがあるが、仕様として致命的。セクション番号はレビュー統合キーやIssue参照に使う前提があるため、番号重複は機械処理を破壊する。"
    suggestion: "即座に再採番し、以後セクション番号をIDとして使うなら一意性を保証する規約（自動検査）を入れよ。"

  - id: m-2
    severity: minor
    section: "§2.2"
    title: "表形式がMarkdownだが、実装でそのまま引用すると2000字制限や整形崩れが起きる"
    description: "Discord通知やエージェント送信で表を多用すると読めない/崩れる。仕様の『通知テンプレート』は実際の媒体制約（Discord 2000字、改行保持/非保持）に合わせて設計すべき。"
    suggestion: "通知テンプレートは箇条書きベースで規格化し、最大長超過時の分割方針も規定せよ。"

  - id: m-3
    severity: minor
    section: "§5.1"
    title: "初回プロンプトに {spec_content} を入れると巨大化しやすい（MAX_EMBED_CHARS戦略が無い）"
    description: "現行 devbar は `MAX_EMBED_CHARS` を持ち、埋め込みが長い場合はtruncationを行う。一方 spec mode は spec全文送付が前提で、長大specで送信失敗・遅延・コスト増になる。"
    suggestion: "サイズ上限と分割戦略（章ごとに分割送信、またはURL/パス参照＋要約）を仕様に含めよ。"

  - id: m-4
    severity: minor
    section: "§8.1"
    title: "Issue起票ルール『注記を削除不可』は運用上強制不能"
    description: "“削除不可”は技術的強制が無い限り規約であり、実装者が誤って消す。watchdogが検査してBLOCKする等の仕組みがないと規約として機能しない。"
    suggestion: "起票後にdevbarが Issue本文を読み戻して注記存在を検査し、欠落なら自動で追記/またはBLOCKする、等を設計に入れよ。"

  - id: s-1
    severity: suggestion
    section: "§5.3"
    title: "severity/verdictの正規化ルール（大小・別名）を先に固定すべき"
    description: "現行 devbar は verdict を upper で比較している。spec mode でも `P0/P1/APPROVE` と `critical/major/...` の対応が必要。今の仕様だと 'Major' 等の表記揺れで集計が壊れる。"
    suggestion: "受理する値の列挙と正規化（例: case-insensitive、絵文字付きも許容）を仕様化し、パーサはそれにだけ従う。"

  - id: s-2
    severity: suggestion
    section: "§11"
    title: "通知イベントに『失敗系』（パース失敗/起票失敗/権限不足）が無い"
    description: "運用で最も詰まるのは失敗時。成功通知だけだと止まっている理由が分からない。"
    suggestion: "最低限: パース失敗、レビュアー送信失敗、git push失敗、glab issue create失敗、queue書き込み失敗、をDiscordへ通知する仕様を追加せよ。"
```

---

## 補足（仕様の“穴”を塞ぐための観点）

- **spec_modeの排他**は、単にフラグで分岐するだけでは不十分です。既存コードは state 名で分岐し、さらに `batch` の形にも強く依存しているため、spec mode を“既存の枠に収める”か“別枠に分ける”かを先に決めないと全てが曖昧になります。
- **決定性**（同じ入力→同じ遷移）はオーケストレータの生命線です。LLMフォールバックでパース/統合/終了判定の結果が揺れる設計は、最終的に人間が毎回監視することになり、自動化の目的に反します。

