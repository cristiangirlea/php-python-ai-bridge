#!/bin/sh
# Checks push-guard.sh on the spellings it must refuse (exit 2, one line on stderr) and the ones it
# must let through (exit 0). Each case is the hook's JSON input; commands are written JSON-escaped.
set -u
hook="$(cd "$(dirname "$0")" && pwd)/push-guard.sh"
fail() { printf 'push-guard.test: %s\n' "$1" >&2; exit 1; }
tmp=$(mktemp -d) || fail "mktemp failed"
trap 'rm -rf "$tmp"' EXIT

# The owner's own git configuration must not change the outcome.
GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL="$tmp/gitconfig"
export GIT_CONFIG_NOSYSTEM GIT_CONFIG_GLOBAL
: > "$tmp/gitconfig"
g() { git -c user.name=t -c user.email=t@example.com "$@"; }

# onmain: on main. onfeat: on my-branch, with a local tag v1.0 and a known remote branch
# old-feature. upmain: on feat, whose push destination is origin/main (push.default=upstream).
for r in onmain onfeat upmain; do
  mkdir "$tmp/$r" && g -C "$tmp/$r" init -q && g -C "$tmp/$r" checkout -q -b main &&
    g -C "$tmp/$r" commit -q --allow-empty -m init &&
    g -C "$tmp/$r" remote add origin https://example.invalid/r.git || fail "setting up $r failed"
done
g -C "$tmp/onfeat" checkout -q -b my-branch && g -C "$tmp/onfeat" tag v1.0 &&
  g -C "$tmp/onfeat" update-ref refs/remotes/origin/old-feature HEAD &&
  g -C "$tmp/onfeat" config alias.st status &&
  g -C "$tmp/onfeat" config alias.ship "push origin main" &&
  g -C "$tmp/onfeat" config alias.yolo "!git push --force origin my-branch" || fail "setting up onfeat failed"
g -C "$tmp/upmain" checkout -q -b feat && g -C "$tmp/upmain" update-ref refs/remotes/origin/main HEAD &&
  g -C "$tmp/upmain" config branch.feat.remote origin &&
  g -C "$tmp/upmain" config branch.feat.merge refs/heads/main &&
  g -C "$tmp/upmain" config push.default upstream || fail "setting up upmain failed"
onmain=$tmp/onmain onfeat=$tmp/onfeat upmain=$tmp/upmain
# onfeat also knows old-feature on a remote named up/stream, and has aliases deep1 to deep7, each
# naming the next. detached: on no branch.
g -C "$onfeat" update-ref refs/remotes/up/stream/old-feature HEAD || fail "setting up up/stream failed"
for i in 1 2 3 4 5 6; do
  g -C "$onfeat" config "alias.deep$i" "deep$((i + 1))" || fail "setting up the aliases failed"
done
g -C "$onfeat" config alias.deep7 status || fail "setting up the aliases failed"
detached=$tmp/detached
g clone -q "$onmain" "$detached" && g -C "$detached" checkout -q --detach || fail "setting up detached failed"
# spaced: on feat, in a directory whose name has a space.
spaced="$tmp/with space"
mkdir "$spaced" && g -C "$spaced" init -q && g -C "$spaced" checkout -q -b feat &&
  g -C "$spaced" commit -q --allow-empty -m init || fail "setting up spaced failed"

n=0
# run WANT COMMAND [CWD [TOOL]]: COMMAND is already JSON-escaped.
run() {
  n=$((n + 1))
  printf '{"session_id":"t","transcript_path":"t","cwd":"%s","hook_event_name":"PreToolUse","tool_name":"%s","tool_input":{"command":"%s","description":"d"}}' \
    "${3:-$onfeat}" "${4:-Bash}" "$2" | sh "$hook" 2>"$tmp/err"
  expect "$1" $? "$2"
}
# raw WANT INPUT: INPUT is fed as it is, for the cases where the JSON itself is unusual.
raw() {
  n=$((n + 1))
  printf '%s' "$2" | sh "$hook" 2>"$tmp/err"
  expect "$1" $? "$2"
}
expect() {
  [ "$2" -eq "$1" ] || fail "$3: exit $2, want $1 (stderr: $(cat "$tmp/err"))"
  if [ "$1" -eq 2 ]; then
    [ "$(grep -c '' "$tmp/err")" -eq 1 ] || fail "$3: the reason is not one line: $(cat "$tmp/err")"
  fi
}
refused() { run 2 "$@"; }
allowed() { run 0 "$@"; }

# Pushes whose destination is main.
refused 'git push' "$onmain"
refused 'git push origin' "$onmain"
refused 'git push -u origin' "$onmain"
refused 'git push --set-upstream origin' "$onmain"
refused 'git push origin HEAD' "$onmain"
refused 'git push origin @' "$onmain"
refused 'git push origin main'
refused 'git push origin MAIN'
refused 'git push origin \"main\"'
refused "git push origin 'main'"
refused 'git push origin HEAD:refs/heads/main'
refused 'git push origin HEAD:main'
refused 'git push origin my-branch:main'
refused 'git push origin :main'
refused 'git push origin refs/heads/main'
refused 'git push origin heads/main'
refused 'git push origin --delete main'
refused 'git push origin main 2>&1'
refused 'git push' "$upmain"
refused 'git -c push.default=matching push'
refused "git -C $onmain push"
refused "git --git-dir=$onmain/.git push"
refused "cd $onmain && git push"
refused 'git push origin :'
refused "git push origin 'refs/heads/*:refs/heads/*'"
# Everything at once, or tags.
refused 'git push --mirror origin'
refused 'git push --all origin'
refused 'git push --branches origin'
refused 'git push --tags'
refused 'git push origin --tags'
refused 'git push --follow-tags origin my-branch'
refused 'git push origin tag v1.0'
refused 'git push origin refs/tags/v1.0'
refused 'git push origin v1.0'
refused 'git push origin --delete v1.0'
# Force pushes of any kind.
refused 'git push --force origin my-branch'
refused 'git push -f origin my-branch'
refused 'git push origin my-branch -f'
refused 'git push -uf origin my-branch'
refused 'git push origin +my-branch'
refused 'git push origin +HEAD:my-branch'
# What a bare push sends is decided by configuration, or cannot be told.
refused 'git -c remote.origin.push=HEAD:refs/heads/x push'
refused 'git -c remote.origin.mirror=true push'
refused 'git push' "$detached"
# Deleting is let through only for a branch the remote is known to have.
refused 'git push origin --delete no-such-branch'
refused 'git push up/stream --delete old-feature'
refused 'git push --repo=https://example.invalid/r.git --delete old-feature'
refused 'git push --force-with-lease origin my-branch'
refused 'git push --force-with-lease=my-branch:abc origin my-branch'
refused 'git push --force-if-includes origin my-branch'
refused 'git push --forc origin my-branch'
# Destinations the hook cannot see are refused.
refused 'git push origin $BRANCH'
refused 'git push origin $(git branch --show-current)'
refused 'git push origin `git branch --show-current`'
refused 'echo main | xargs git push origin'
refused 'git $sub origin main'
refused '$git push origin main'
refused 'eval $cmd; echo pushed'
refused 'iex $cmd; echo pushed' . PowerShell
refused 'echo 5 | xargs gh pr merge'
refused 'echo x | xargs gh api -X POST repos/o/r/pulls'
refused 'gh api -X POST repos/o/r/$path'
# Tags and releases.
refused 'git tag v1.1'
refused 'git tag -a v1.1 -m release'
refused 'git tag -l'
refused 'gh release create v1.0'
refused 'gh release list'
refused 'gh pr merge 5 --auto --merge'
refused 'gh pr merge 5 --merge --admin'
refused 'gh -R o/r pr merge 5 --admin'
refused 'gh pr -R o/r merge 5 --auto'
refused 'gh api -X POST repos/o/r/releases'
refused 'gh api --method PATCH repos/o/r/releases/1'
refused 'gh api --method=PUT repos/o/r/git/refs/heads/main'
refused 'gh api -XDELETE repos/o/r/git/refs/heads/main'
refused 'gh api repos/o/r/git/refs -f ref=refs/heads/main -f sha=abc'
refused 'gh api repos/o/r/releases -F tag_name=v1'
refused 'gh api repos/o/r/releases --input body.json'
refused 'gh api -H \"Accept: application/json\" -X POST repos/o/r/releases'
refused 'gh api --jq .ref -f ref=refs/heads/x repos/o/r/git/refs'
# Compound commands and wrappers.
refused 'git status && git push origin main'
refused 'git status || git push origin main'
refused 'git status; git push origin main'
refused 'git status | git push origin main'
refused 'git status\ngit push origin main'
refused 'git status\r\ngit tag v2'
refused 'git push origin \\\nmain'
refused 'git push origin `\r\nmain' . PowerShell
refused 'GIT_TRACE=1 git push origin main'
refused 'env GIT_TRACE=1 git push origin main'
refused 'sudo git push origin main'
refused 'GIT push origin main'
refused "bash -c 'git push origin main'"
refused 'sh -c \"git tag v2\"'
refused 'bash -lc \"git status; git push --force origin my-branch\"'
refused 'powershell -NoProfile -Command \"git push --force origin my-branch\"' . PowerShell
refused "pwsh -c 'gh release create v2'" . PowerShell
refused 'powershell -EncodedCommand ZwBpAHQA' . PowerShell
refused 'cmd /c git push origin main' . PowerShell
refused '& git push origin main' . PowerShell
refused '& \"C:\\Program Files\\Git\\cmd\\git.exe\" push origin main' . PowerShell
refused 'strace -f /usr/bin/git push origin main'
refused 'git push origin main; if ($?) { echo ok }' . PowerShell
refused '$out = git push origin main' . PowerShell
refused "Start-Process git -ArgumentList 'push','origin','main'" . PowerShell
refused 'Invoke-Expression \"git tag v3\"' . PowerShell
# Command substitutions run, in double quotes too; a comment ends at the line's end.
refused 'echo \"$(git push origin main)\"'
refused 'echo \"`git tag v2`\"'
refused 'git commit -m \"x $(date) y\" && git push origin main'
refused 'git push origin \"$(git branch --show-current)\"'
refused "git commit -m 'a; b' && git push origin main"
refused "git status # don't\\ngit push origin main"
refused 'echo ${#x}; git push origin main'
refused 'cd \"C:\\x\\\"; git push origin main' . PowerShell
# A heredoc's body is text up to its word; what follows it is read again.
refused 'cat <<EOF >body.md\nhi\nEOF\ngit push origin main'
refused 'cat <<EOF; git push origin main\nhi\nEOF'
refused 'cat <<EOF\n$(git push origin main)\nEOF'
# A keyword before a command.
refused 'if true; then git push origin main; fi'
refused 'while true; do git tag v1; done'
refused 'if false; then :; else git push origin main; fi'
refused 'if false; then :; elif git push origin main; then :; fi'
refused 'if git push origin main; then :; fi'
refused 'until git tag v1; do :; done'
allowed 'if true; then git status; else git log; fi'
# Git aliases are expanded.
refused 'git ship'
refused 'git yolo'
refused 'git deep1'

# Allowed: feature-branch pushes and ordinary commands.
allowed 'git push -u origin my-branch'
allowed 'git push origin my-branch'
allowed 'git push --set-upstream origin my-branch'
allowed 'git push -u origin HEAD'
allowed 'git push origin HEAD:my-branch'
allowed 'git push origin hrn/guard-hook:guard-hook'
allowed 'git push'
allowed 'git push origin'
allowed 'git push origin --delete old-feature'
allowed 'git push -o ci.skip origin --delete old-feature'
allowed 'git push --receive-pack git-receive-pack origin --delete old-feature'
allowed 'git push -u origin my-branch 2>&1 | tail -5'
allowed 'git status'
allowed 'git st'
allowed 'git log --oneline -5'
allowed 'git checkout main && git pull origin main'
allowed 'git fetch --tags origin'
allowed 'git log --grep=tag'
allowed 'git commit -m \"explain why we never git push to main\"'
allowed 'grep -rn \"git push origin main\" .'
allowed 'echo git push origin main'
# Quoted text is not a command.
allowed 'git commit -m \"docs: guard (git tag, git push)\"'
allowed 'gh pr create --title x --body \"refuses \\`git tag\\`\"'
allowed 'echo \"ok; git push origin main\"'
allowed 'git commit -m \"line one\nline two; git push origin main\"'
allowed "bash -c \\\"git commit -m 'x; git push origin main'\\\""
allowed 'echo \"a`; git tag b\"' . PowerShell
allowed "git commit -F - <<EOF\\ngit tag v1 is the owner's step\\nEOF\\ngit status"
allowed "gh pr create --title x --body-file - <<'EOF'\\nRefuses \`git push origin main\`; (and git tag).\\nEOF"
refused 'cat <<-EOF >x\n\tgit tag v1\n\tEOF\ngit push origin main'
refused 'cat <<A <<B\nx\nA\ny\nB\ngit push origin main'
refused 'cat <<<\"x\"; git push origin main'
refused 'x=$((1<<3))\ngit push origin main'
# << in arithmetic is a shift, whatever follows it; a heredoc's word may start with a letter.
refused 'x=$((1<<n))\ngit push origin main'
refused 'x=$((a<<b))\ngit push origin main'
refused '((x<<y))\ngit push origin main'
refused 'echo $((1<<bits)); git tag v1'
refused 'echo \"$((1<<n))\"\ngit push origin main'
refused 'echo $(( (1<<n) + $(date) ))\ngit push origin main'
allowed 'echo $((1<<n))'
allowed 'cat <<n\ngit push origin main\nn\ngit status'
refused 'cat <<EOF\nx\nEOF\ngit push origin main'
refused 'cmd=\"git push origin main\"; eval $cmd'
refused 'git status \u0026\u0026 git push origin main'
allowed 'cat <<A <<B\ngit tag x\nA\ngit push origin main\nB\ngit status'
allowed 'cat <<A <<B\nx\nB\ngit push origin main\nA\ngit status'
allowed 'cat <<\"EOF\"\ngit push origin main\nEOF'
# A multi-kilobyte heredoc body with quotes, backslashes and guarded words, and what follows it.
big='See \"git push origin main\" and '\''git tag v1'\'' under C:\\dir\\file.\n'
for _ in 1 2 3 4 5 6; do big=$big$big; done
allowed "gh pr create --title x --body-file - <<'EOF'\\n${big}EOF"
refused "gh pr create --title x --body-file - <<'EOF'\\n${big}EOF\\ngit push origin main"
refused "gh pr create --title x --body-file - <<EOF\\n${big}\$(git push origin main)\\nEOF"
allowed 'eval \"$(echo git status)\"' "$tmp/push/clone"
allowed 'echo push'
allowed 'go test ./...'
allowed 'go vet ./... && go build ./...'
allowed 'gh pr create --title x --body-file body.md'
allowed 'gh pr merge 5 --merge --delete-branch --match-head-commit abc'
allowed 'gh pr view 5'
allowed 'gh api repos/o/r/releases'
allowed 'gh api -X GET repos/o/r/git/refs -f per_page=5'
allowed 'gh api repos/o/r/pulls'
allowed "gh api graphql -f query='query(\$owner:String!){viewer{login}}' -F owner=x"
allowed 'gh api repos/o/r/issues/1/comments -f body=\"see releases\"'
allowed '$env:GIT_TERMINAL_PROMPT = \"0\"; git status' . PowerShell
allowed 'Get-ChildItem -Recurse | Select-String push' . PowerShell
allowed ''
# A quoted path with a space is one word.
allowed "cd \\\"$spaced\\\" && git push origin feat"
allowed "git -C \\\"$spaced\\\" push origin feat"

# The input itself: pretty-printed JSON is read; input the hook cannot read is refused only when
# it mentions a push, a tag, a release, a merge or the API.
raw 2 '{
  "cwd": "'"$onfeat"'",
  "tool_name": "Bash",
  "tool_input": {
    "command": "git push origin main"
  }
}'
raw 2 'not json: git push origin main'
raw 2 '{"tool_input":{"command":"git push origin main'
raw 2 '{"tool_input":{"command":null},"note":"release"}'
raw 0 'not json: ls -la'
raw 0 '{"tool_input":{"command":"ls -la'
raw 0 ''
# A \u escape the hook would read as a space is part of a word to the shell, so a # after it is no
# comment, and escapes can spell a guarded word: any escape but & < > ' is refused.
refused 'echo\u001f#; git push origin main'
refused 'echo\u0001#; git push origin main'
refused 'echo\u007f#; git push origin main'
refused 'echo\u263a#; git push origin main'
refused 'echo\ud83d\ude00#; git push origin main'
refused 'git \u0070ush origin main'
refused 'echo\u001f#; ls'
allowed 'ls \u0026\u0026 echo \u0027a\u0027 \u003e out \u003C in'
# A lone carriage return ends a line in PowerShell, so a # after it starts a comment there.
refused 'git push origin main\r# comment' . PowerShell
allowed 'git status\r\ngit log' . PowerShell
# The cwd is decoded too: an escape the hook cannot decode faithfully there is refused, an escaped
# backslash (a Windows path) is not, and neither is a literal backslash and u in the command.
raw 2 '{"cwd":"/tmp/a\u0001b","tool_input":{"command":"git push"}}'
grep -q 'the cwd holds' "$tmp/err" || fail "the cwd case was refused for another reason: $(cat "$tmp/err")"
raw 2 '{"cwd":"/tmp/a\nb","tool_input":{"command":"git push"}}'
grep -q 'the cwd holds' "$tmp/err" || fail "the cwd case was refused for another reason: $(cat "$tmp/err")"
raw 0 '{"cwd":"C:\\new\\x","tool_input":{"command":"git status"}}'
allowed 'echo \\u263a; git status'
# Bash keeps a CR inside a word, so for Bash any CR is refused; a backspace or form feed is
# refused for both shells.
refused 'echo\r#; git push origin main'
refused 'git push origin \\\r\nmain'
refused 'echo\b#; git push origin main' . PowerShell
refused 'echo\f#; git push origin main'
# A cwd with any escape but a quote or a slash is refused: a tab there is no space.
raw 2 '{"cwd":"/tmp/a\tb","tool_input":{"command":"git push"}}'
grep -q 'the cwd holds' "$tmp/err" || fail "the cwd case was refused for another reason: $(cat "$tmp/err")"
raw 2 '{"cwd":"/tmp/a\fb","tool_input":{"command":"git push"}}'
grep -q 'the cwd holds' "$tmp/err" || fail "the cwd case was refused for another reason: $(cat "$tmp/err")"
# A raw separator byte would turn into a backslash when the hook decodes escapes.
us=$(printf '\037')
raw 2 "{\"tool_input\":{\"command\":\"echo a$us; git push origin main \\u0026\"}}"
# A failure of the hook itself fails closed for a command that mentions a push: here git kills it.
mkdir "$tmp/shim" && printf '#!/bin/sh\nkill -TERM "$PPID"\n' > "$tmp/shim/git" &&
  printf '#!/bin/sh\nexit 0\n' > "$tmp/shim/probe" && chmod +x "$tmp/shim/git" "$tmp/shim/probe" ||
  fail "setting up the git shim failed"
# The shim needs an executable temporary directory; the test containers mount /tmp noexec, and there this case is skipped.
if "$tmp/shim/probe" 2>/dev/null; then
  n=$((n + 1))
  printf '{"cwd":"%s","tool_name":"Bash","tool_input":{"command":"git push origin my-branch"}}' "$onfeat" |
    PATH="$tmp/shim:$PATH" sh "$hook" 2>"$tmp/err"
  # The shell may add its own line for the signal, so only the exit and the reason are checked.
  [ $? -eq 2 ] && grep -q 'the hook failed' "$tmp/err" ||
    fail "git push under a git that kills the hook: not refused as a failure: $(cat "$tmp/err")"
else
  echo "push-guard.test: skipped the killed-git case: $tmp is not executable"
fi
# In heredoc_skip, an unusable TMPDIR fails closed when a guarded word is mentioned and open otherwise.
n=$((n + 1))
printf '{"cwd":"%s","tool_name":"Bash","tool_input":{"command":"cat <<EOF\\nhi\\nEOF\\ngit push origin my-branch"}}' "$onfeat" |
  TMPDIR="$tmp/no-such-dir" sh "$hook" 2>"$tmp/err"
expect 2 $? "TMPDIR failure on git push"
grep -q 'the hook failed (status 3)' "$tmp/err" ||
  fail "TMPDIR failure was not refused with status 3: $(cat "$tmp/err")"
n=$((n + 1))
printf '{"cwd":"%s","tool_name":"Bash","tool_input":{"command":"cat <<EOF\\nhi\\nEOF\\ngit status"}}' "$onfeat" |
  TMPDIR="$tmp/no-such-dir" sh "$hook" 2>"$tmp/err"
expect 0 $? "TMPDIR failure on git status"
# A Windows cwd, backslashes JSON-escaped, as Claude Code sends it on Windows.
if command -v cygpath >/dev/null 2>&1; then
  winmain=$(cygpath -w "$onmain" | sed 's/\\/\\\\/g')
  refused 'git push' "$winmain"
  winfeat=$(cygpath -w "$onfeat" | sed 's/\\/\\\\/g')
  allowed 'git push' "$winfeat"
  winspaced=$(cygpath -m "$spaced")
  allowed "cd \\\"$winspaced\\\" && git push origin feat"
fi

# settings.json runs the hook before every Bash and PowerShell call.
settings="$(dirname "$hook")/../settings.json"
tr -d ' \t\r\n' < "$settings" |
  grep -qF '"PreToolUse":[{"matcher":"Bash|PowerShell","hooks":[{"type":"command","command":"sh\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/push-guard.sh"' ||
  fail "settings.json does not run push-guard.sh before Bash and PowerShell calls"

echo "push-guard.test: ok ($n cases)"
