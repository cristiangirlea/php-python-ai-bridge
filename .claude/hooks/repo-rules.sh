#!/bin/sh
# After an edit: what CONTRIBUTING.md requires always to hold in the file just edited.
# PHP under src/, examples/ or tests/php declares strict types and uses spaces; a worker module
# (worker/ai_bridge) imports no model dependency at module level, since that path is standard library only.
# Exit 2 reports the problem to the agent; the edit itself has already happened.
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
input=$(cat)
file=$(printf '%s' "$input" | sed -n 's/.*"file_path"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)
[ -n "$file" ] || exit 0
# The path arrives absolute, on Windows with backslashes (JSON-escaped as \\) and a drive letter of either
# case; make it repository-relative.
file=$(printf '%s' "$file" | sed 's#\\\\#/#g; s#\\#/#g')
root=$(pwd -W 2>/dev/null || pwd)
upper() { printf '%s' "$1" | awk '{ print toupper(substr($0, 1, 1)) substr($0, 2) }'; }
case $file in
  [A-Za-z]:/*) file=$(upper "$file"); root=$(upper "$root") ;;
esac
case $file in
  "$root"/*) file=${file#"$root"/} ;;
  "$PWD"/*) file=${file#"$PWD"/} ;;
esac
[ -f "$file" ] || exit 0
msg=""
case $file in
  src/*.php|examples/*.php|tests/php/*.php)
    if ! head -n 10 "$file" | grep -q 'declare(strict_types=1);'; then
      msg="$file: add declare(strict_types=1); right after the opening tag (CONTRIBUTING.md)."
    fi
    if grep -q "$(printf '\t')" "$file"; then
      msg="${msg:+$msg
}$file: indent with four spaces, not tabs (CONTRIBUTING.md)."
    fi
    ;;
  worker/ai_bridge/*.py)
    # Any import statement naming a model dependency outside a function body: at column zero, or
    # indented under try/if at module level. Imports inside functions are indented too, so an indented
    # match is only reported when no def precedes it.
    deps='(numpy|onnxruntime|tokenizers|torch|transformers)'
    if awk -v re="^[[:space:]]*(import[[:space:]]+([A-Za-z_.]+[[:space:]]*,[[:space:]]*)*${deps}([[:space:].,]|$)|from[[:space:]]+${deps}([[:space:].]|$))" '
        /^[[:space:]]*(def|async[[:space:]]+def|class)[[:space:]]/ { infn = 1 }
        /^[^[:space:]#]/ && !/^(def|async[[:space:]]+def|class)[[:space:]]/ { infn = 0 }
        $0 ~ re && !infn { found = 1 }
        END { exit found ? 0 : 1 }' "$file"; then
      msg="$file: a model dependency is imported at module level; import it inside the ONNX function only (the worker path is standard library only)."
    fi
    ;;
  *) exit 0 ;;
esac
if [ -n "$msg" ]; then
  printf '%s\n' "$msg" >&2
  exit 2
fi
exit 0
