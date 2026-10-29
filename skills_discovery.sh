#!/usr/bin/env sh
# =============================================================================
# skills-discovery.sh - find AI agent skills folder on this machine
#
# Probes project, user and system skill roots for Claude Code, claude.ai /
# Cowork / Skills API, Antigravity, Codex, GitHub Copilot, Cursor, Amp, Cline,
# OpenCode, Warp and ADK, then prints one line per skill.
#
# Read-only. POSIX sh, no dependencies. Linux, macOS, WSL and Windows
# (Git Bash / MSYS2 / Cygwin).
# =============================================================================
set -u

SHOW_DESC=1; SHOW_EMPTY=0; MANIFESTS=0; USE_COLOR=1; TARGET=""; FILTER=""

usage() {
  cat <<'EOF'
Usage: skills-discovery.sh [OPTIONS] [WORKSPACE_DIR]

  -d, --no-desc            hide description snippets
  -e, --show-empty         also list probed roots that are absent or empty
  -m, --include-manifests  also read skills declared in skills.json manifests
  -H, --harness NAME       filter: claude, claude.ai, codex, copilot, cursor,
                           antigravity, amp, cline, opencode, warp, adk
  -n, --no-color           plain output
  -h, --help               this message
EOF
}

while [ $# -gt 0 ]; do
  case $1 in
    -d|--no-desc)           SHOW_DESC=0 ;;
    -e|--show-empty)        SHOW_EMPTY=1 ;;
    -m|--include-manifests) MANIFESTS=1 ;;
    -n|--no-color)          USE_COLOR=0 ;;
    -H|--harness)
      [ $# -ge 2 ] || { echo "error: --harness needs a value" >&2; exit 1; }
      FILTER=$(printf '%s' "$2" | tr '[:upper:]' '[:lower:]'); shift ;;
    -h|--help)              usage; exit 0 ;;
    -*)                     echo "unknown option: $1" >&2; usage >&2; exit 1 ;;
    *)                      TARGET=$1 ;;
  esac
  shift
done

# -----------------------------------------------------------------------------
# 1. Palette, glyphs, terminal width
# -----------------------------------------------------------------------------
if [ "$USE_COLOR" -eq 1 ] && [ -z "${NO_COLOR:-}" ]; then
  E=$(printf '\033'); R="$E[0m"; B="$E[1m"; D="$E[2m"; IT="$E[3m"
  _fg() { printf '%s[38;5;%sm' "$E" "$1"; }
  RED=$(_fg 203);   CORAL=$(_fg 209); ORANGE=$(_fg 215); AMBER=$(_fg 222)
  YELLOW=$(_fg 227); LIME=$(_fg 155); GREEN=$(_fg 84);   TEAL=$(_fg 43)
  CYAN=$(_fg 87);   BLUE=$(_fg 75);   PURPLE=$(_fg 141); MAGENTA=$(_fg 177)
  PINK=$(_fg 212);  WHITE=$(_fg 255); GRAY=$(_fg 245)
  P_LOCAL="$E[48;5;23;38;5;159;1m"; P_USER="$E[48;5;53;38;5;219;1m"
  P_SYS="$E[48;5;52;38;5;210;1m"
else
  R= B= D= IT= RED= CORAL= ORANGE= AMBER= YELLOW= LIME= GREEN= TEAL= CYAN=
  BLUE= PURPLE= MAGENTA= PINK= WHITE= GRAY= P_LOCAL= P_USER= P_SYS=
fi

# Box glyphs unless the locale is explicitly a single-byte one. ASCII=1 forces plain.
case "${ASCII:-}${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" in
  1*|C|POSIX|C.*|*ISO8859*|*iso8859*) TMID='+' TEND='+' RULE='-' DOT='-' NE='!=' ELL='>' ;;
  *)                                  TMID='├' TEND='└' RULE='─' DOT='·' NE='≠' ELL='…' ;;
esac

WIDTH=$(tput cols 2>/dev/null || printf '0')
case $WIDTH in ''|*[!0-9]*) WIDTH=0 ;; esac
[ "$WIDTH" -ge 70 ] || WIDTH=100
[ "$WIDTH" -le 140 ] || WIDTH=140
NAME_W=$((WIDTH / 3))
[ "$NAME_W" -le 34 ] || NAME_W=34
ROOT_W=$((WIDTH - 44))
DESC_W=$((WIDTH - NAME_W - 12))

repeat() { _o=''; _i=0; while [ "$_i" -lt "$2" ]; do _o="$_o$1"; _i=$((_i + 1)); done; printf '%s' "$_o"; }

clip() { # text width -> at most that many columns, ellipsis when cut
  if [ ${#1} -gt "$2" ]; then printf '%.'"$(($2 - 1))"'s%s' "$1" "$ELL"
  else printf '%s' "$1"; fi
}

fit() { # text width -> exactly that many columns, padded or clipped
  clip "$1" "$2"
  [ ${#1} -lt "$2" ] && repeat ' ' $(($2 - ${#1}))
  return 0
}

section() { # title color
  _t=" $1 "
  printf '\n%s%s%s%s%s%s%s\n' "$B$2" "$RULE$RULE" "$R$B$WHITE" "$_t" \
    "$R$2" "$(repeat "$RULE" $((WIDTH - ${#_t} - 2)))" "$R"
}

# -----------------------------------------------------------------------------
# 2. Harness keys. The key is also the label; the color makes it scannable.
# -----------------------------------------------------------------------------
ALL='universal claude claude.ai codex copilot cursor antigravity amp cline opencode warp adk'

badge() {
  case $1 in
    universal)   _c=$TEAL ;;    claude)   _c=$ORANGE ;;  claude.ai) _c=$AMBER ;;
    codex)       _c=$GREEN ;;   copilot)  _c=$PURPLE ;;  cursor)    _c=$BLUE ;;
    antigravity) _c=$CYAN ;;    amp)      _c=$PINK ;;    cline)     _c=$CORAL ;;
    opencode)    _c=$LIME ;;    warp)     _c=$MAGENTA ;; adk)       _c=$YELLOW ;;
    *)           _c=$GRAY ;;
  esac
  printf '%s%s%s' "$_c" "$1" "$R"
}

badges() { _bf=1; for _k in $1; do [ "$_bf" -eq 1 ] || printf ' '; badge "$_k"; _bf=0; done; }

pill() { # fixed width so every root line aligns
  case $1 in
    LOCAL)  printf '%s LOCAL  %s' "$P_LOCAL" "$R" ;;
    SYSTEM) printf '%s SYSTEM %s' "$P_SYS" "$R" ;;
    *)      printf '%s USER   %s' "$P_USER" "$R" ;;
  esac
}

# -----------------------------------------------------------------------------
# 3. Platform detection and base directories
# -----------------------------------------------------------------------------
UNAME_S=$(uname -s 2>/dev/null || echo Unknown)
IS_WSL=0; IS_WIN=0
case "$UNAME_S" in
  Linux*)
    if [ -n "${WSL_DISTRO_NAME:-}" ] || [ -f /proc/sys/fs/binfmt_misc/WSLInterop ]; then
      OS_FAMILY="linux (wsl)"; IS_WSL=1
    else OS_FAMILY="linux"; fi ;;
  Darwin*)                        OS_FAMILY="macos" ;;
  CYGWIN*|MINGW*|MSYS*|Windows_NT*) OS_FAMILY="windows ($UNAME_S)"; IS_WIN=1 ;;
  *)
    if [ "${OS:-}" = "Windows_NT" ]; then OS_FAMILY="windows"; IS_WIN=1
    else OS_FAMILY=$(printf '%s' "$UNAME_S" | tr '[:upper:]' '[:lower:]'); fi ;;
esac

norm() { # backslashes -> slashes, via cygpath where available
  [ -n "${1:-}" ] || return 0
  if [ "$IS_WIN" -eq 1 ] && command -v cygpath >/dev/null 2>&1; then
    cygpath -u "$1" 2>/dev/null || printf '%s' "$1" | tr '\\' '/'
  else
    printf '%s' "$1" | tr '\\' '/'
  fi
}

HOME_D=$(norm "${HOME:-${USERPROFILE:-}}")
XDG_D=$(norm "${XDG_CONFIG_HOME:-$HOME_D/.config}")
MAC_D=""
[ -d "$HOME_D/Library/Application Support" ] && MAC_D="$HOME_D/Library/Application Support"
WIN_D=""; APPD=""; LAPPD=""; PROGD=""

if [ "$IS_WIN" -eq 1 ]; then
  WIN_D=$(norm "${USERPROFILE:-$HOME_D}")
  APPD=$(norm "${APPDATA:-$WIN_D/AppData/Roaming}")
  LAPPD=$(norm "${LOCALAPPDATA:-$WIN_D/AppData/Local}")
  PROGD=$(norm "${PROGRAMDATA:-${ALLUSERSPROFILE:-C:/ProgramData}}")
elif [ "$IS_WSL" -eq 1 ]; then
  if command -v cmd.exe >/dev/null 2>&1 && command -v wslpath >/dev/null 2>&1; then
    _up=$(cmd.exe /c "echo %USERPROFILE%" 2>/dev/null | tr -d '\r\n')
    [ -n "$_up" ] && [ "$_up" != '%USERPROFILE%' ] && WIN_D=$(wslpath -u "$_up" 2>/dev/null || true)
  fi
  [ -z "$WIN_D" ] && [ -d "/mnt/c/Users/${USER:-}" ] && WIN_D="/mnt/c/Users/$USER"
  if [ -n "$WIN_D" ] && [ -d "$WIN_D" ]; then
    APPD="$WIN_D/AppData/Roaming"; LAPPD="$WIN_D/AppData/Local"; PROGD="/mnt/c/ProgramData"
  fi
fi

# Environment overrides. Empty means the variable is unset, and the table skips it.
ENV_CLAUDE=""; [ -n "${CLAUDE_CONFIG_DIR:-}" ] && ENV_CLAUDE=$(norm "$CLAUDE_CONFIG_DIR")
ENV_CODEX="";  [ -n "${CODEX_HOME:-}" ]        && ENV_CODEX=$(norm "$CODEX_HOME")
ENV_GEMINI=""; [ -n "${GEMINI_HOME:-}" ]       && ENV_GEMINI=$(norm "$GEMINI_HOME")
ENV_AG="";     [ -n "${AG_SKILLS_DIR:-}" ]     && ENV_AG=$(norm "$AG_SKILLS_DIR")
ENV_ADK="";    [ -n "${ADK_SKILLS_DIR:-}" ]    && ENV_ADK=$(norm "$ADK_SKILLS_DIR")

if [ -n "$TARGET" ]; then
  WORKSPACE=$(cd "$TARGET" 2>/dev/null && pwd) || WORKSPACE=$TARGET
else
  WORKSPACE=$(pwd)
fi
REPO=""
command -v git >/dev/null 2>&1 &&
  REPO=$(cd "$WORKSPACE" 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null || true)
[ -z "$REPO" ] && command -v hg >/dev/null 2>&1 &&
  REPO=$(cd "$WORKSPACE" 2>/dev/null && hg root --pager=never -y 2>/dev/null || true)

# -----------------------------------------------------------------------------
# 4. SKILL.md parsing
# -----------------------------------------------------------------------------
frontmatter() { # file field -> first value
  awk -v field="$2" '
    { sub(/\r$/, "") }
    NR == 1 && /^---[ \t]*$/ { fm = 1; next }
    fm && /^---[ \t]*$/ { exit }
    fm {
      if ($0 ~ "^" field ":[ \t]*") {
        sub("^" field ":[ \t]*", "", $0)
        gsub(/^["'\''>|+-]+|["'\'']+$/, "", $0)
        gsub(/^[ \t]+|[ \t]+$/, "", $0)
        if (length($0)) { print; exit }
        multi = 1; next
      }
      if (multi) {
        if ($0 ~ /^[a-zA-Z0-9_-]+:/) exit
        gsub(/^[ \t]+|[ \t]+$/, "", $0)
        if (length($0)) { print; exit }
      }
    }' "$1" 2>/dev/null
}

bang_commands() { # file -> "L<n>: <directive>" for !`cmd` and line-leading !cmd
  awk '
    { sub(/\r$/, "") }
    NR == 1 && /^---[ \t]*$/ { fm = 1; next }
    fm && /^---[ \t]*$/ { fm = 0; next }
    fm { next }
    /^[ \t]*```/ { fence = !fence; next }
    {
      # directives inside a fence are usually documentation, but whether a
      # harness honors the fence is its own business, so flag them either way
      mark = fence ? "  [in code fence]" : ""
      # inline !`command`, pairing backticks left to right so a trailing
      # backtick in `foo!` is never read as an opening directive
      rest = $0; prev = ""
      while ((q1 = index(rest, "`")) > 0) {
        open = 0
        if (q1 > 1 && substr(rest, q1 - 1, 1) == "!") {
          before = (q1 > 2) ? substr(rest, q1 - 2, 1) : prev
          if (before == "" || before ~ /[ \t(:>-]/) open = 1
        }
        tail = substr(rest, q1 + 1)
        if ((q2 = index(tail, "`")) == 0) break
        inner = substr(tail, 1, q2 - 1)
        if (open && inner ~ /^[a-zA-Z0-9_.\/~$-]/) printf "L%d: !`%s`%s\n", NR, inner, mark
        prev = "`"; rest = substr(tail, q2 + 1)
      }
      # line-leading !command, excluding ![images] and [!NOTE] alerts
      if ($0 ~ /^[ \t]*![a-zA-Z0-9_.\/~$-]/) {
        t = $0; sub(/^[ \t]+/, "", t); printf "L%d: %s%s\n", NR, t, mark
      }
    }' "$1" 2>/dev/null
}

skill_scripts() { # skill dir -> relative paths, executables marked with *
  _sd="$1/scripts"; [ -d "$_sd" ] || return 0
  find "$_sd" -type f 2>/dev/null | sort | while IFS= read -r _f; do
    _rel=${_f#"$_sd"/}
    [ -x "$_f" ] && _rel="$_rel*"
    printf '%s\n' "$_rel"
  done
}

skill_files() { # root -> every SKILL.md at depth 0, 1 or 2
  _r="$1"
  if [ -f "$_r/SKILL.md" ]; then printf '%s\n' "$_r/SKILL.md"; return 0; fi
  for _d in "$_r"/*/ "$_r"/.[!.]*/; do
    [ -d "$_d" ] || continue
    _d=${_d%/}
    if   [ -f "$_d/SKILL.md" ]; then printf '%s\n' "$_d/SKILL.md"
    elif [ -f "$_d/skill.md" ]; then printf '%s\n' "$_d/skill.md"
    else
      # nested groups such as ~/.codex/skills/.system/<skill>/SKILL.md.
      # synced/ is registered as a root of its own, so skip it here.
      case $(basename "$_d") in synced) continue ;; esac
      for _s in "$_d"/*/; do
        [ -d "$_s" ] && [ -f "${_s%/}/SKILL.md" ] && printf '%s\n' "${_s%/}/SKILL.md"
      done
    fi
  done
}

short() { # absolute path -> ./relative or ~/relative
  case $1 in
    "$WORKSPACE"/*) printf './%s' "${1#"$WORKSPACE"/}" ;;
    "$HOME_D"/*)    printf '~/%s' "${1#"$HOME_D"/}" ;;
    *)              printf '%s' "$1" ;;
  esac
}

# -----------------------------------------------------------------------------
# 5. Candidate roots
#    Tables are scope-agnostic: one row per documented path, with the harnesses
#    that read it. Adding a harness means adding rows, not code.
# -----------------------------------------------------------------------------
CAND=$(mktemp "${TMPDIR:-/tmp}/skroots.XXXXXX")
EXECF=$(mktemp "${TMPDIR:-/tmp}/skexec.XXXXXX")
STATS=$(mktemp "${TMPDIR:-/tmp}/skstat.XXXXXX")
MISS=$(mktemp "${TMPDIR:-/tmp}/skmiss.XXXXXX")
trap 'rm -f "$CAND" "$EXECF" "$STATS" "$MISS"' EXIT INT TERM
SEEN='|'

add_root() { # scope primary compat path [origin tag]
  [ -n "$4" ] || return 0
  if [ -n "$FILTER" ]; then
    case $(printf '%s %s' "$2" "$3" | tr '[:upper:]' '[:lower:]') in
      *"$FILTER"*) ;; *) return 0 ;;
    esac
  fi
  if [ -d "$4" ]; then _real=$(cd "$4" 2>/dev/null && pwd -P) || _real=$4; else _real=$4; fi
  case "$SEEN" in *"|$_real|"*) return 0 ;; esac
  SEEN="$SEEN$_real|"
  printf '%s|%s|%s|%s|%s\n' "$1" "$2" "$3" "${5:-}" "$4" >> "$CAND"
}

# 5a. Project / workspace roots, relative to a base directory
scan_local() { # base tag
  [ -n "$1" ] || return 0
  while IFS='|' read -r primary compat rel; do
    [ -n "${rel:-}" ] && add_root LOCAL "$primary" "$compat" "$1/$rel" "$2"
  done <<EOF
universal|$ALL|.agents/skills
claude|claude claude.ai adk|.claude/skills
claude.ai|claude.ai claude|.claude/skills/synced
codex|codex|.codex/skills
copilot|copilot|.github/skills
copilot|copilot|.copilot/skills
copilot|copilot|.vscode/skills
cursor|cursor|.cursor/skills
antigravity|antigravity|.agent/skills
antigravity|antigravity|.gemini/skills
antigravity|antigravity|.gemini/config/skills
antigravity|antigravity adk|_agents/skills
amp|amp|.amp/skills
cline|cline|.cline/skills
cline|cline|.clinerules/skills
opencode|opencode|.opencode/skills
warp|warp|.warp/skills
adk|adk claude.ai|.adk/skills
adk|adk claude.ai|skills
arm|arm|.arm/skills
EOF
}

scan_local "$WORKSPACE" ""
[ -n "$REPO" ] && [ "$REPO" != "$WORKSPACE" ] && scan_local "$REPO" "repo root"

# 5b. User and system roots. Column 1 picks the base directory:
#     H home  X xdg  M macOS app support  W windows home  A appdata
#     L localappdata  P programdata  S filesystem root
#     C/O/G/V/K environment overrides, skipped when the variable is unset
while IFS='|' read -r tok primary compat rel; do
  tag=''
  case $tok in
    H) base=$HOME_D;  scope=USER ;;
    X) base=$XDG_D;   scope=USER ;;
    M) base=$MAC_D;   scope=USER ;;
    W) base=$WIN_D;   scope=USER ;;
    A) base=$APPD;    scope=USER ;;
    L) base=$LAPPD;   scope=USER ;;
    P) base=$PROGD;   scope=SYSTEM ;;
    S) base=/;        scope=SYSTEM ;;
    C) base=$ENV_CLAUDE; scope=USER; tag='$CLAUDE_CONFIG_DIR' ;;
    O) base=$ENV_CODEX;  scope=USER; tag='$CODEX_HOME' ;;
    G) base=$ENV_GEMINI; scope=USER; tag='$GEMINI_HOME' ;;
    V) base=$ENV_AG;     scope=USER; tag='$AG_SKILLS_DIR' ;;
    K) base=$ENV_ADK;    scope=USER; tag='$ADK_SKILLS_DIR' ;;
    *) continue ;;
  esac
  [ -n "$base" ] || continue
  if [ -n "${rel:-}" ]; then path="${base%/}/$rel"; else path="${base%/}"; fi
  add_root "$scope" "$primary" "$compat" "$path" "$tag"
done <<EOF
H|universal|$ALL|.agents/skills
X|universal|amp antigravity codex opencode adk|agents/skills
C|claude|claude claude.ai|skills
H|claude|claude claude.ai adk|.claude/skills
H|claude.ai|claude.ai claude|.claude/skills/synced
X|claude|claude claude.ai|claude/skills
X|claude.ai|claude.ai claude|Claude/skills
O|codex|codex|skills
H|codex|codex|.codex/skills
X|codex|codex|codex/skills
H|copilot|copilot|.copilot/skills
X|copilot|copilot|github-copilot/skills
X|copilot|copilot|copilot/skills
H|cursor|cursor|.cursor/skills
H|cursor|cursor|.cursor/skills-cursor
X|cursor|cursor|Cursor/User/skills
V|antigravity|antigravity|
G|antigravity|antigravity|config/skills
G|antigravity|antigravity|antigravity-cli/skills
H|antigravity|antigravity|.gemini/config/skills
H|antigravity|antigravity|.gemini/antigravity-cli/skills
H|antigravity|antigravity|.gemini/antigravity/skills
H|antigravity|antigravity|.gemini/skills
H|antigravity|antigravity|.agent/skills
H|antigravity|antigravity|.antigravity/skills
H|antigravity|antigravity|.agy/antigravity-cli/skills
H|antigravity|antigravity|.gemini/jetski/builtin/skills
H|antigravity|antigravity|.gemini/jetski/builtin/contrib/skills
X|amp|amp|amp/skills
H|amp|amp|.amp/skills
H|cline|cline|.cline/skills
X|cline|cline|cline/skills
X|opencode|opencode|opencode/skills
H|opencode|opencode|.opencode/skills
H|warp|warp|.warp/skills
X|warp|warp|warp/skills
K|adk|adk|
H|adk|adk claude.ai|.adk/skills
X|adk|adk claude.ai|adk/skills
X|arm|arm|arm/skills
H|arm|arm|.arm/skills
M|claude|claude claude.ai|ClaudeCode/.claude/skills
M|claude.ai|claude.ai claude|Claude/skills
M|cursor|cursor|Cursor/User/skills
M|copilot|copilot|GitHub Copilot/skills
M|copilot|copilot|Code/User/skills
M|antigravity|antigravity|Antigravity/skills
M|warp|warp|dev.warp.Warp-Stable/skills
W|universal|$ALL|.agents/skills
W|claude|claude claude.ai adk|.claude/skills
W|claude.ai|claude.ai claude|.claude/skills/synced
W|codex|codex|.codex/skills
W|copilot|copilot|.copilot/skills
W|cursor|cursor|.cursor/skills
W|cursor|cursor|.cursor/skills-cursor
W|antigravity|antigravity|.gemini/config/skills
W|antigravity|antigravity|.gemini/antigravity-cli/skills
W|antigravity|antigravity|.gemini/antigravity/skills
W|antigravity|antigravity|.gemini/skills
W|antigravity|antigravity|.agent/skills
W|amp|amp|.config/amp/skills
W|amp|amp|.amp/skills
W|cline|cline|.cline/skills
W|opencode|opencode|.config/opencode/skills
W|opencode|opencode|.opencode/skills
W|warp|warp|.warp/skills
W|adk|adk claude.ai|.adk/skills
A|claude|claude claude.ai|Claude Code/skills
A|claude|claude claude.ai|ClaudeCode/.claude/skills
A|claude.ai|claude.ai claude|Claude/skills
A|codex|codex|codex/skills
A|copilot|copilot|GitHub Copilot/skills
A|copilot|copilot|Code/User/skills
A|cursor|cursor|Cursor/User/skills
A|cursor|cursor|Cursor/skills
A|antigravity|antigravity|Antigravity/skills
A|amp|amp|amp/skills
A|cline|cline|Cline/skills
A|opencode|opencode|opencode/skills
A|warp|warp|Warp/skills
A|adk|adk|adk/skills
L|claude|claude claude.ai|Claude Code/skills
L|claude.ai|claude.ai claude|Claude/skills
L|claude.ai|claude.ai claude|Packages/Claude_pzs8sxrjxfjjc/LocalCache/Roaming/Claude/skills
L|antigravity|antigravity|Antigravity/skills
L|codex|codex|codex/skills
L|copilot|copilot|GitHub Copilot/skills
L|warp|warp|Warp/Warp/data/skills
L|adk|adk|adk/skills
S|codex|codex|etc/codex/skills
S|claude|claude claude.ai|etc/claude-code/.claude/skills
S|claude|claude claude.ai|etc/claude/.claude/skills
S|antigravity|antigravity|etc/gemini/skills
S|claude|claude claude.ai|Library/Application Support/ClaudeCode/.claude/skills
S|codex|codex|Library/Application Support/codex/skills
P|claude|claude claude.ai|ClaudeCode/.claude/skills
P|claude|claude claude.ai|Claude Code/skills
P|codex|codex|codex/skills
P|antigravity|antigravity|Antigravity/skills
EOF

# 5c. Plugin bundles ship their own skills directory
for _pd in "$HOME_D/.gemini/config/plugins"/*/skills "$HOME_D/.claude/plugins"/*/skills; do
  [ -d "$_pd" ] || continue
  case $_pd in
    *.gemini*) add_root USER antigravity antigravity "$_pd" "plugin" ;;
    *.claude*) add_root USER claude "claude claude.ai" "$_pd" "plugin" ;;
  esac
done

# -----------------------------------------------------------------------------
# 6. Header
# -----------------------------------------------------------------------------
PROBED=$(awk 'END {print NR+0}' "$CAND")
section "AI SKILLS DISCOVERY" "$BLUE"
printf ' %s%s%s %s %s%s%s' "$GREEN" "$OS_FAMILY" "$R" "$DOT" "$CYAN" "$(short "$WORKSPACE")" "$R"
[ -n "$REPO" ] && [ "$REPO" != "$WORKSPACE" ] &&
  printf ' %s %srepo %s%s' "$DOT" "$D" "$(short "$REPO")" "$R"
printf ' %s %s%s roots probed%s\n' "$DOT" "$D" "$PROBED" "$R"
printf ' %sflags%s  %sT%s allowed-tools  %s!%s !command  %sS%s scripts/  %sr%s references/  %s@%s symlink  %sA%s adk-meta  %s~%s name%sdir\n' \
  "$D" "$R" "$RED" "$R" "$ORANGE" "$R" "$TEAL" "$R" "$BLUE" "$R" \
  "$MAGENTA" "$R" "$YELLOW" "$R" "$GRAY" "$R" "$NE"

# -----------------------------------------------------------------------------
# 7. Render
# -----------------------------------------------------------------------------
TOTAL=0; ROOTS=0; N_LOCAL=0; N_USER=0; N_SYS=0

render_root() { # scope primary compat tag root skills
  _scope=$1; _prim=$2; _compat=$3; _tag=$4; _root=$5; _list=$6
  _n=0
  [ -n "$_list" ] && _n=$(printf '%s\n' "$_list" | awk 'END {print NR}')

  printf '\n'
  pill "$_scope"
  printf ' %s%s%s %s%2d%s ' "$B$WHITE" "$(fit "$(short "$_root")" "$ROOT_W")" "$R" "$B$GREEN" "$_n" "$R"
  badges "$_compat"
  [ -n "$_tag" ] && printf ' %s(%s)%s' "$D" "$_tag" "$R"
  printf '\n'

  [ -n "$_list" ] || { printf ' %s%s empty%s\n' "$GRAY" "$TEND" "$R"; return 0; }

  _i=0
  printf '%s\n' "$_list" | while IFS= read -r md; do
    [ -n "$md" ] || continue
    _i=$((_i + 1))
    dir=$(dirname "$md"); folder=$(basename "$dir")
    name=$(frontmatter "$md" name)
    desc=$(frontmatter "$md" description)
    [ "$_i" -eq "$_n" ] && tree=$TEND || tree=$TMID

    # flags: plain copy drives the column width, colored copy gets printed
    f=''; cf=''; scripts=0; bang=0; tools=0
    if grep -qE '^allowed[-_]tools:' "$md" 2>/dev/null; then
      tools=1; f="${f}T"; cf="$cf$B$RED""T$R"
    fi
    cmds=$(bang_commands "$md")
    [ -n "$cmds" ] && { bang=1; f="${f}!"; cf="$cf$B$ORANGE""!$R"; }
    [ -d "$dir/scripts" ] && { scripts=1; f="${f}S"; cf="$cf$B$TEAL""S$R"; }
    [ -d "$dir/references" ] && { f="${f}r"; cf="$cf$BLUE""r$R"; }
    [ -L "$dir" ] && { f="${f}@"; cf="$cf$MAGENTA""@$R"; }
    grep -qE 'adk_additional_tools|adk_inject_state' "$md" 2>/dev/null &&
      { f="${f}A"; cf="$cf$YELLOW""A$R"; }
    [ -n "$name" ] && [ "$name" != "$folder" ] && { f="${f}~"; cf="$cf$GRAY""~$R"; }

    printf '%d|%d|%d\n' "$scripts" "$bang" "$tools" >> "$STATS"
    [ "$scripts" -eq 1 ] || [ "$bang" -eq 1 ] &&
      printf '%s|%s|%s|%d|%d|%s\n' "$_scope" "$folder" "$_compat" "$scripts" "$bang" "$md" >> "$EXECF"

    printf ' %s%s%s %s%s%s %s' "$GRAY" "$tree" "$R" "$GREEN" "$(fit "$folder" "$NAME_W")" "$R" "$cf"
    if [ "$SHOW_DESC" -eq 1 ] && [ -n "$desc" ]; then
      printf '%s %s%s%s' "$(repeat ' ' $((7 - ${#f})))" "$D$IT" "$(clip "$desc" "$DESC_W")" "$R"
    fi
    printf '\n'
  done
}

while IFS='|' read -r scope primary compat tag root; do
  if [ ! -e "$root" ]; then
    printf '%s|absent|%s\n' "$scope" "$root" >> "$MISS"
    continue
  fi
  list=$(skill_files "$root")
  if [ -z "$list" ]; then
    printf '%s|empty|%s\n' "$scope" "$root" >> "$MISS"
    [ "$SHOW_EMPTY" -eq 1 ] || continue
  fi
  n=0
  [ -n "$list" ] && n=$(printf '%s\n' "$list" | awk 'END {print NR}')
  ROOTS=$((ROOTS + 1)); TOTAL=$((TOTAL + n))
  case $scope in
    LOCAL)  N_LOCAL=$((N_LOCAL + n)) ;;
    SYSTEM) N_SYS=$((N_SYS + n)) ;;
    *)      N_USER=$((N_USER + n)) ;;
  esac
  render_root "$scope" "$primary" "$compat" "$tag" "$root" "$list"
done < "$CAND"

# skills.json manifests point at directories outside any documented root
if [ "$MANIFESTS" -eq 1 ]; then
  for sj in "$WORKSPACE/_agents/skills.json" "$WORKSPACE/configs/jetski/_agents/skills.json" \
            "$HOME_D/.gemini/config/skills.json"; do
    [ -f "$sj" ] || continue
    mlist=''
    for entry in $(awk -F'"' '/"path"[ \t]*:/ {print $4}' "$sj" 2>/dev/null); do
      res=$entry
      [ ! -d "$res" ] && [ -n "$REPO" ] && [ -d "$REPO/$entry" ] && res="$REPO/$entry"
      [ -f "$res/SKILL.md" ] && mlist="${mlist}${mlist:+
}$res/SKILL.md"
    done
    [ -n "$mlist" ] || continue
    mn=$(printf '%s\n' "$mlist" | awk 'END {print NR}')
    ROOTS=$((ROOTS + 1)); TOTAL=$((TOTAL + mn))
    case $sj in
      "$WORKSPACE"*) msc=LOCAL; N_LOCAL=$((N_LOCAL + mn)) ;;
      *)             msc=USER;  N_USER=$((N_USER + mn)) ;;
    esac
    render_root "$msc" antigravity "antigravity adk" manifest "$sj" "$mlist"
  done
fi

# -----------------------------------------------------------------------------
# 8. Executable skills: a scripts/ folder or a !command directive
# -----------------------------------------------------------------------------
N_EXEC=0; N_SCRIPTS=0; N_BANG=0; N_TOOLS=0
if [ -s "$STATS" ]; then
  eval "$(awk -F'|' '
    { if ($1 == 1 || $2 == 1) e++; if ($1 == 1) s++; if ($2 == 1) b++; if ($3 == 1) t++ }
    END { printf "N_EXEC=%d N_SCRIPTS=%d N_BANG=%d N_TOOLS=%d\n", e+0, s+0, b+0, t+0 }' "$STATS")"
fi

section "EXECUTABLE SKILLS $DOT $N_EXEC of $TOTAL" "$YELLOW"
if [ "$N_EXEC" -eq 0 ]; then
  printf ' %snone: every skill found is instructions only%s\n' "$GRAY" "$R"
else
  while IFS='|' read -r e_scope e_name e_compat e_scripts e_bang e_md; do
    [ -n "$e_md" ] || continue
    e_dir=$(dirname "$e_md")
    printf '\n'
    pill "$e_scope"
    printf ' %s%s%s %s%s%s ' "$B$GREEN" "$(fit "$e_name" "$NAME_W")" "$R" \
      "$CYAN" "$(clip "$(short "$e_dir")" $((WIDTH - NAME_W - 32)))" "$R"
    badges "$e_compat"
    printf '\n'
    if [ "$e_scripts" -eq 1 ]; then
      files=$(skill_scripts "$e_dir" | awk '{printf "  %s", $0} END {printf "\n"}')
      [ -n "$files" ] || files='  (empty)'
      printf '   %s%sS%s %s%s%s\n' "$B" "$TEAL" "$R" "$WHITE" "$(clip "$files" $((WIDTH - 6)))" "$R"
    fi
    if [ "$e_bang" -eq 1 ]; then
      bang_commands "$e_md" | head -n 6 | while IFS= read -r bc; do
        [ -n "$bc" ] && printf '   %s%s!%s %s%s%s\n' "$B" "$ORANGE" "$R" "$ORANGE" \
          "$(clip "$bc" $((WIDTH - 6)))" "$R"
      done
    fi
  done < "$EXECF"
fi

# -----------------------------------------------------------------------------
# 9. Summary
# -----------------------------------------------------------------------------
section SUMMARY "$BLUE"
if [ "$TOTAL" -eq 0 ]; then
  printf ' %sno skills found in %s probed roots%s  (%s-e%s lists every path checked)\n' \
    "$YELLOW" "$PROBED" "$R" "$B" "$R"
else
  printf '  %sOverall Skills Found:%s      %s%d total skill(s)%s across %s%d active folder(s)%s\n' \
    "$B$WHITE" "$R" "$B$GREEN" "$TOTAL" "$R" "$B$CYAN" "$ROOTS" "$R"
  printf '  %sSkills with Executables:%s   %s%d executable skill(s)%s of %s%d total%s (%s%d instruction-only%s)\n' \
    "$B$WHITE" "$R" "$B$YELLOW" "$N_EXEC" "$R" "$B$WHITE" "$TOTAL" "$R" \
    "$GRAY" "$((TOTAL - N_EXEC))" "$R"
  printf '    %s%s%s With "scripts/" dir:%s  %s%d skill(s)%s\n' \
    "$D" "$TMID" "$RULE" "$R" "$B$TEAL" "$N_SCRIPTS" "$R"
  printf '    %s%s%s With "!command":%s      %s%d skill(s)%s\n' \
    "$D" "$TMID" "$RULE" "$R" "$B$ORANGE" "$N_BANG" "$R"
  printf '    %s%s%s Declares allowed-tools:%s %s%d skill(s)%s\n' \
    "$D" "$TEND" "$RULE" "$R" "$B$RED" "$N_TOOLS" "$R"
  printf '  %sSkills by Scope:%s           %sLocal/Project: %d%s  |  %sUser/Global: %d%s  |  %sSystem: %d%s\n' \
    "$B$WHITE" "$R" "$CYAN" "$N_LOCAL" "$R" "$MAGENTA" "$N_USER" "$R" "$RED" "$N_SYS" "$R"
fi

if [ "$SHOW_EMPTY" -eq 1 ] && [ -s "$MISS" ]; then
  section "PROBED, NOTHING THERE" "$GRAY"
  while IFS='|' read -r m_scope m_state m_root; do
    printf ' %s%-7s %-52.52s %s%s\n' "$D" "$m_scope" "$(short "$m_root")" "$m_state" "$R"
  done < "$MISS"
fi
printf '\n'
