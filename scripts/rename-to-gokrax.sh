#!/bin/bash
# rename-to-gokrax.sh — gokrax → gokrax 一括リネームスクリプト
#
# Usage:
#   bash scripts/rename-to-gokrax.sh --dry-run   # 変更一覧を表示（何も変更しない）
#   bash scripts/rename-to-gokrax.sh --apply      # 実行
#
# 対象: Tier 1 (gokraxリポ本体) + Tier 2 (shared/)
# 非対象: 他エージェントworkspace, GitLab, Discord

set -euo pipefail

# ============================================================
# 設定
# ============================================================
REPO_DIR="/mnt/s/wsl/work/project/gokrax"
SHARED_DIR="$HOME/.openclaw/shared"

# テキスト置換ルール（順序重要: 長い方から先にマッチさせる）
# gokrax は固有名詞として常に小文字で統一
declare -a SED_RULES=(
    's/GOKRAX/GOKRAX/g'
    's/gokrax/gokrax/g'
    's/gokrax/gokrax/g'
    's/gokrax/gokrax/g'
)

# テキスト置換対象の拡張子
TEXT_EXTENSIONS="py|md|sh|txt|json|yaml|yml|toml|cfg"

# ファイルリネーム対象（旧名 → 新名）
declare -A FILE_RENAMES=(
    # Tier 1: リポ内
    ["$REPO_DIR/gokrax.py"]="$REPO_DIR/gokrax.py"
    ["$REPO_DIR/gokrax-queue.txt"]="$REPO_DIR/gokrax-queue.txt"
    # Tier 2: shared
    ["$SHARED_DIR/pipelines/gokrax.json"]="$SHARED_DIR/pipelines/gokrax.json"
    ["$SHARED_DIR/gokrax-state.json"]="$SHARED_DIR/gokrax-state.json"
    ["$SHARED_DIR/gokrax-metrics.jsonl"]="$SHARED_DIR/gokrax-metrics.jsonl"
)

# シンボリックリンク更新
declare -A SYMLINK_UPDATES=(
    ["$SHARED_DIR/bin/gokrax"]="$SHARED_DIR/bin/gokrax"
)

# ディレクトリリネーム（最後に実行）
# 注: NTFSはcase-insensitive。gokrax→gokrax は一時名経由が必要
REPO_DIR_NEW="/mnt/s/wsl/work/project/gokrax"
REPO_DIR_TMP="/mnt/s/wsl/work/project/_gokrax_rename_tmp"

# 除外パターン
EXCLUDE_DIRS=".git|__pycache__|.pytest_cache|node_modules"

# ============================================================
# ヘルパー
# ============================================================
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

DRY_RUN=true

usage() {
    echo "Usage: $0 [--dry-run|--apply]"
    echo ""
    echo "  --dry-run   変更一覧を表示（デフォルト、何も変更しない）"
    echo "  --apply     実際に変更を適用"
    exit 1
}

log_action() {
    local action="$1" detail="$2"
    if $DRY_RUN; then
        echo -e "  ${YELLOW}[DRY]${NC} $action: $detail"
    else
        echo -e "  ${GREEN}[OK]${NC} $action: $detail"
    fi
}

log_section() {
    echo ""
    echo -e "${CYAN}=== $1 ===${NC}"
}

# ============================================================
# Phase 1: テキスト置換の対象ファイルを収集
# ============================================================
collect_text_targets() {
    local dir="$1"
    find "$dir" -type f \
        -regextype posix-extended \
        -regex ".*\\.($TEXT_EXTENSIONS)" \
        | grep -Ev "($EXCLUDE_DIRS)" \
        | sort
}

show_text_changes() {
    local file="$1"
    # 各ルールでマッチする行を表示
    local has_match=false
    while IFS= read -r line; do
        local lineno="${line%%:*}"
        local content="${line#*:}"
        echo -e "    ${RED}-${NC} L${lineno}: ${content}"
        # 置換後を計算
        local replaced="$content"
        for rule in "${SED_RULES[@]}"; do
            replaced=$(echo "$replaced" | sed "$rule")
        done
        echo -e "    ${GREEN}+${NC} L${lineno}: ${replaced}"
        has_match=true
    done < <(grep -n -E "GOKRAX|gokrax|gokrax|gokrax" "$file" 2>/dev/null || true)
    $has_match
}

# ============================================================
# Phase 2: テキスト置換を実行
# ============================================================
apply_text_replace() {
    local file="$1"
    local sed_expr=""
    for rule in "${SED_RULES[@]}"; do
        sed_expr="${sed_expr} -e '${rule}'"
    done
    eval sed -i $sed_expr "'$file'"
}

# ============================================================
# メイン
# ============================================================
case "${1:---dry-run}" in
    --dry-run) DRY_RUN=true ;;
    --apply)   DRY_RUN=false ;;
    *)         usage ;;
esac

echo -e "${CYAN}================================================${NC}"
echo -e "${CYAN}  gokrax → gokrax リネームスクリプト${NC}"
echo -e "${CYAN}================================================${NC}"
if $DRY_RUN; then
    echo -e "${YELLOW}  MODE: ドライラン（変更なし）${NC}"
else
    echo -e "${RED}  MODE: 本番適用${NC}"
    echo ""
    read -p "  本当に実行しますか？ (yes/no): " confirm
    if [[ "$confirm" != "yes" ]]; then
        echo "中止しました。"
        exit 0
    fi
fi

# --- Step 1: テキスト置換 ---
log_section "Step 1: テキスト置換"

TEXT_CHANGE_COUNT=0
FILE_CHANGE_COUNT=0

# Tier 1: リポ本体
log_section "Tier 1: gokraxリポ ($REPO_DIR)"
while IFS= read -r file; do
    if grep -qE "GOKRAX|gokrax|gokrax|gokrax" "$file" 2>/dev/null; then
        rel="${file#$REPO_DIR/}"
        log_action "REPLACE" "$rel"
        if $DRY_RUN; then
            show_text_changes "$file" && FILE_CHANGE_COUNT=$((FILE_CHANGE_COUNT + 1)) || true
            count=$(grep -cE "GOKRAX|gokrax|gokrax|gokrax" "$file" 2>/dev/null || echo 0)
            TEXT_CHANGE_COUNT=$((TEXT_CHANGE_COUNT + count)) || true
        else
            apply_text_replace "$file"
            FILE_CHANGE_COUNT=$((FILE_CHANGE_COUNT + 1))
        fi
    fi
done < <(collect_text_targets "$REPO_DIR")

# Tier 2: shared/
log_section "Tier 2: shared/ ($SHARED_DIR)"
for target in \
    "$SHARED_DIR/pipelines/gokrax.json" \
    "$SHARED_DIR/gokrax-state.json" \
    "$SHARED_DIR/gokrax-metrics.jsonl" \
    "$SHARED_DIR/knowledge.md" \
    ; do
    if [[ -f "$target" ]] && grep -qE "GOKRAX|gokrax|gokrax|gokrax" "$target" 2>/dev/null; then
        rel="${target#$SHARED_DIR/}"
        log_action "REPLACE" "$rel"
        if $DRY_RUN; then
            show_text_changes "$target" && FILE_CHANGE_COUNT=$((FILE_CHANGE_COUNT + 1)) || true
            count=$(grep -cE "GOKRAX|gokrax|gokrax|gokrax" "$target" 2>/dev/null || echo 0)
            TEXT_CHANGE_COUNT=$((TEXT_CHANGE_COUNT + count)) || true
        else
            apply_text_replace "$target"
            FILE_CHANGE_COUNT=$((FILE_CHANGE_COUNT + 1))
        fi
    fi
done

# --- Step 2: ファイルリネーム ---
log_section "Step 2: ファイルリネーム"

for old in "${!FILE_RENAMES[@]}"; do
    new="${FILE_RENAMES[$old]}"
    if [[ -e "$old" ]]; then
        log_action "RENAME" "$old → $new"
        if ! $DRY_RUN; then
            mv "$old" "$new"
        fi
    else
        echo -e "  ${RED}[SKIP]${NC} not found: $old"
    fi
done

# --- Step 3: シンボリックリンク更新 ---
log_section "Step 3: シンボリックリンク更新"

for old in "${!SYMLINK_UPDATES[@]}"; do
    new="${SYMLINK_UPDATES[$old]}"
    if [[ -L "$old" ]]; then
        target=$(readlink "$old")
        # シンボリックリンクの参照先も更新
        new_target="${target//gokrax/gokrax}"
        new_target="${new_target//gokrax/gokrax}"
        log_action "SYMLINK" "$old → $new (-> $new_target)"
        if ! $DRY_RUN; then
            rm "$old"
            ln -s "$new_target" "$new"
        fi
    else
        echo -e "  ${RED}[SKIP]${NC} not a symlink: $old"
    fi
done

# --- Step 4: /tmp ファイル & watchdog 停止 ---
log_section "Step 4: watchdog 停止 & /tmp クリーンアップ"

# リネーム前の PIDFILE で起動した watchdog を停止
OLD_PIDFILE="/tmp/devbar-watchdog-loop.pid"
if [[ -f "$OLD_PIDFILE" ]]; then
    OLD_PID=$(cat "$OLD_PIDFILE" 2>/dev/null)
    if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
        log_action "KILL" "旧 watchdog-loop (PID $OLD_PID from $OLD_PIDFILE)"
        if ! $DRY_RUN; then
            kill "$OLD_PID" 2>/dev/null || true
            rm -f "$OLD_PIDFILE"
        fi
    else
        echo -e "  ${YELLOW}[INFO]${NC} $OLD_PIDFILE exists but process not running"
        if ! $DRY_RUN; then
            rm -f "$OLD_PIDFILE"
        fi
    fi
else
    echo -e "  ${YELLOW}[INFO]${NC} $OLD_PIDFILE not found（既に停止済み）"
fi

# 旧 /tmp ファイルをクリーンアップ
for old_tmp in /tmp/devbar-watchdog-loop.lock /tmp/devbar-watchdog.lock /tmp/devbar-watchdog.log; do
    if [[ -e "$old_tmp" ]]; then
        log_action "CLEANUP" "$old_tmp"
        if ! $DRY_RUN; then
            rm -f "$old_tmp"
        fi
    fi
done
if [[ -d "/tmp/devbar-review" ]]; then
    log_action "CLEANUP" "/tmp/devbar-review/"
    if ! $DRY_RUN; then
        rm -rf "/tmp/devbar-review"
    fi
fi

# --- Step 5: ディレクトリリネーム ---
log_section "Step 5: ディレクトリリネーム"
log_action "RENAME" "$REPO_DIR → $REPO_DIR_NEW (via tmp)"
if ! $DRY_RUN; then
    # NTFSはcase-insensitive。gokrax→gokrax は直接mvで可能（異なる文字列なので）
    # ただし安全のため一時名経由
    mv "$REPO_DIR" "$REPO_DIR_TMP"
    mv "$REPO_DIR_TMP" "$REPO_DIR_NEW"
    echo -e "  ${YELLOW}[NOTE]${NC} リポのパスが変わりました。以降のコマンドは $REPO_DIR_NEW で実行してください"
fi

# --- Step 6: crontab 更新 ---
log_section "Step 6: crontab 更新"
if crontab -l 2>/dev/null | grep -q "gokrax"; then
    log_action "CRONTAB" "gokrax → gokrax"
    if ! $DRY_RUN; then
        crontab -l 2>/dev/null | sed 's/gokrax/gokrax/g; s/gokrax/gokrax/g' | crontab -
    else
        echo "  現在のcrontabエントリ:"
        crontab -l 2>/dev/null | grep "gokrax" | while IFS= read -r line; do
            echo -e "    ${RED}-${NC} $line"
            echo -e "    ${GREEN}+${NC} $(echo "$line" | sed 's/gokrax/gokrax/g; s/gokrax/gokrax/g')"
        done
    fi
else
    echo "  crontab に gokrax エントリなし（スキップ）"
fi

# --- Step 7: GitLab remote更新 ---
log_section "Step 7: GitLab remote（手動）"
echo "  以下を手動で実行:"
echo "    1. GitLabでリポ名変更: atakalive/gokrax → atakalive/gokrax"
echo "    2. git remote set-url origin git@gitlab.com:atakalive/gokrax.git"
echo "    3. leibniz workspace内のgokraxクローンも更新"
echo "    4. GOKRAX_DRY_RUN 環境変数を使用している箇所があれば GOKRAX_DRY_RUN に変更"
echo "    5. エージェント workspace の MEMORY.md / memory/*.md 内の gokrax 参照は後日対応"

# --- サマリー ---
log_section "サマリー"
if $DRY_RUN; then
    echo -e "  テキスト置換: ${YELLOW}${TEXT_CHANGE_COUNT}行${NC} in ${FILE_CHANGE_COUNT}ファイル"
    echo -e "  ファイルリネーム: ${YELLOW}${#FILE_RENAMES[@]}件${NC}"
    echo -e "  シンボリックリンク: ${YELLOW}${#SYMLINK_UPDATES[@]}件${NC}"
    echo -e "  ディレクトリリネーム: ${YELLOW}1件${NC}"
    echo ""
    echo -e "  ${GREEN}実行するには: bash scripts/rename-to-gokrax.sh --apply${NC}"
else
    echo -e "  ${GREEN}完了！${NC}"
    echo ""
    echo -e "  ${YELLOW}次のステップ:${NC}"
    echo "    1. cd $REPO_DIR_NEW && pytest tests/ -v"
    echo "    2. GitLabリポ名を変更"
    echo "    3. git remote set-url origin git@gitlab.com:atakalive/gokrax.git"
    echo "    4. watchdog-loop.sh を再起動"
    echo "    5. GOKRAX_DRY_RUN 環境変数の外部参照を GOKRAX_DRY_RUN に更新"
    echo "    6. エージェント workspace の gokrax 参照を後日更新"
fi
