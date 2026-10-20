#!/bin/sh
# Before a Bash or PowerShell call: refuse what only the owner does. That is a git push whose
# destination is main or a tag, a force push, any git tag, any gh release, gh pr merge with --auto
# or --admin, and a mutating gh api call on releases or git refs. Exit 2 blocks the call and shows
# the one-line reason; everything else exits 0.
# settings.json's deny rules match spellings, so a new spelling gets through. This reads what the
# command does, and refuses when it cannot tell. It runs under Git Bash on Windows and sh on Linux,
# before every call, so a command without a trigger word exits before any other process starts.
# Past that: sed for JSON escapes, tr for capitals, git to resolve a push or an alias, and a
# subshell for the hook's body and for each shell, eval, alias and heredoc whose text it checks.
LC_ALL=C
# Git Bash's C is UTF-8, where bash's pattern cuts and sed's s///g cost the whole text per step: a
# single-byte locale, as C is elsewhere, keeps a long command fast.
case ${OSTYPE-} in msys* | cygwin*) LC_ALL=C.ISO-8859-1 ;; esac
export LC_ALL
set -f
nl='
'

# One line: a pretty-printed input's newlines are only between JSON tokens.
input=
while IFS= read -r line || [ -n "$line" ]; do input="$input $line"; done

refuse() {
  printf 'push-guard: %s. This is the owner'"'"'s step: hand over the command.\n' "$1" >&2
  exit 2
}
# mentions TEXT: TEXT names a push, a tag, a release, a merge or the API.
mentions() {
  case $1 in
    *[Pp][Uu][Ss][Hh]* | *[Tt][Aa][Gg]* | *[Rr][Ee][Ll][Ee][Aa][Ss][Ee]* | *[Mm][Ee][Rr][Gg][Ee]* | *[Aa][Pp][Ii]*) return 0 ;;
  esac
  return 1
}
# lower WORD: sets lc to WORD in lower case; tr runs only when there is a capital.
lower() {
  case $1 in
    *[A-Z]*) lc=$(printf '%s' "$1" | tr 'A-Z' 'a-z') ;;
    *) lc=$1 ;;
  esac
}
# prog WORD: sets w to the program WORD names, without its directory or .exe, in lower case.
prog() {
  p=${1##*/}
  lower "${p##*\\}"
  w=${lc%.exe}
}

# json_string KEY: sets val to the still-escaped string value of "KEY": in the input, or fails.
# The value ends at the first quote that an odd run of backslashes does not escape.
json_string() {
  rest=$input
  while :; do
    case $rest in *"\"$1\""*) ;; *) return 1 ;; esac
    rest=${rest#*"\"$1\""}
    t=${rest#"${rest%%[![:space:]]*}"}
    case $t in :*) ;; *) continue ;; esac # the key's name was a value: look further
    t=${t#:}
    t=${t#"${t%%[![:space:]]*}"}
    case $t in '"'*) t=${t#\"} ;; *) return 1 ;; esac
    val=
    # Split at quotes once: cutting a long value one quote at a time is quadratic in bash. The x
    # keeps a field after a final quote, so every field but the last is followed by one.
    oifs=$IFS
    IFS='"'
    set -- ${t}x
    IFS=$oifs
    while [ $# -gt 1 ]; do
      head=$1
      shift
      h=$head
      while :; do case $h in *'\\') h=${h%??} ;; *) break ;; esac; done
      case $h in
        *'\') val=$val$head'"' ;;
        *) val=$val$head; return 0 ;;
      esac
    done
    return 1
  done
}
# unescape: JSON escapes decoded, a line at a time. An escaped backslash is set aside first, so it
# cannot start another escape. A backslash or backtick before a newline joins the lines (sh and
# PowerShell line continuations); other newlines stay. CRLF and a lone CR are newlines (PowerShell
# ends a line at a CR). Control characters: spaces.
unescape() {
  sed -e 's/\\\\/'"$soh"'/g' -e 's/\\r\\n/\\n/g' -e 's/\\r/\\n/g' \
    -e 's/'"$soh"'\\n/ /g' -e 's/`\\n/ /g' -e 's/\\n/\'"$nl"'/g' \
    -e 's/\\t/ /g' -e 's/\\"/"/g' -e 's/\\\//\//g' -e 's/\\[bf]/ /g' \
    -e 's/\\u0026/\&/g' -e 's/\\u003[cC]/</g' -e 's/\\u003[eE]/>/g' -e "s/\\\\u0027/'/g" \
    -e 's/\\u[0-9a-fA-F]\{4\}/ /g' -e 's/'"$soh"'/\\/g'
}

# use_lang LANG: how the text is quoted: sh (\ escapes), ps (PowerShell, ` escapes) or cmd (none,
# and ' is no quote).
use_lang() {
  lang=$1
  case $1 in sh) esc='\' ;; ps) esc='`' ;; *) esc= ;; esac
}

# words TEXT: sets wl to the words of one simple command, each followed by $soh, with one level of
# quotes taken off. Outside quotes, blanks, newlines and commas (PowerShell's argument arrays) end
# a word.
words() {
  ws=$1 wl= wd= wq= wm=o
  while :; do
    case $wm in
      o) wp=${ws%%[\"\'\\\`\ \	,$nl]*} ;;
      s) wp=${ws%%\'*} ;;
      *) wp=${ws%%[\"\\\`]*} ;;
    esac
    wd=$wd$wp ws=${ws#"$wp"}
    [ -n "$ws" ] || break
    wc=${ws%%"${ws#?}"} ws=${ws#?} # %%, not %: see check_cmd
    case $wm$wc in
      s\' | d\") wm=o ;;
      o\') if [ "$lang" = cmd ]; then wd=$wd$wc; else wm=s wq=1; fi ;;
      o\") wm=d wq=1 ;;
      [od]\\ | [od]\`)
        if [ "$wc" = "$esc" ]; then
          wn=${ws%%"${ws#?}"} ws=${ws#?}
          # In sh's double quotes, \ escapes only " \ $ and `.
          case $wm$lang$wn in dsh[\"\\\$\`]) ;; dsh*) wn=$wc$wn ;; esac
          wd=$wd$wn
        else
          wd=$wd$wc
        fi ;;
      *)
        [ -z "$wd$wq" ] || wl=$wl$wd$soh
        wd= wq= ;;
    esac
  done
  [ -z "$wd$wq" ] || wl=$wl$wd$soh
}

# git_in_dir ARGS: git in the directory the command runs in, with the command's own -C, -c and
# --git-dir options. Fails when that directory is unknown.
git_in_dir() {
  [ -n "$dir" ] || return 1
  IFS=$soh
  git -C "$dir" $gopts "$@" 2>/dev/null
  gs=$?
  IFS=' '
  return $gs
}
current_branch() {
  b=$(git_in_dir rev-parse --abbrev-ref HEAD) && [ -n "$b" ] && [ "$b" != HEAD ] && printf '%s' "$b"
}

# check_cmd TEXT: each simple command in TEXT, split at ; | & ( ) { } and newlines outside quotes.
# A command substitution, $( ) or sh's backticks, in double quotes too, is checked as commands, and
# the command around it gets a $ where it was, as what it adds cannot be seen. A comment is skipped,
# and so is a heredoc's body, unless its word is unquoted and the body has a substitution: then the
# body is checked as commands.
check_cmd() {
  cs=$1 cur= md=o stk= lastc= hdq=
  while [ -n "$cs" ]; do
    case $md in
      o) pre=${cs%%[\;\|\&\(\)\{\}\<\#\"\'\\\`$nl]*} ;;
      s) pre=${cs%%\'*} ;;
      *) pre=${cs%%[\"\\\`\(]*} ;;
    esac
    cur=$cur$pre cs=${cs#"$pre"}
    case $pre in ?*) lastc=${pre##"${pre%?}"} ;; esac
    [ -n "$cs" ] || break
    # %% and ## find a one-character cut at once; % and # try every position, quadratic in bash.
    ch=${cs%%"${cs#?}"} cs=${cs#?}
    case $md$ch in
      s\' | d\") cur=$cur$ch md=o ;;
      o\') cur=$cur$ch; [ "$lang" = cmd ] || md=s ;;
      o\") cur=$cur$ch md=d ;;
      [od]\\ | [od]\`)
        if [ "$ch" = "$esc" ]; then
          cur=$cur$ch${cs%%"${cs#?}"} cs=${cs#?}
        elif [ "$ch$lang" = '`sh' ]; then
          case $md$stk in
            o[OD]*) check_part "$cur"; sub_end ;;
            o*) cur="$cur \$"; sub_start O ;;
            *) cur="$cur \$"; sub_start D ;;
          esac
        else
          cur=$cur$ch
        fi ;;
      d\() case $cur in *'$') cur=$cur$ch; sub_start d ;; *) cur=$cur$ch ;; esac ;;
      o\()
        # A ( straight after another one, as in $(( or ((, opens arithmetic, and so does any ( inside
        # it: a << there is a shift.
        case $stk in
          a*) sub_start a ;;
          *) if [ "$lastc" = '(' ] && [ -z "$cur" ]; then sub_start a; else cur=$cur$ch; sub_start o; fi ;;
        esac ;;
      o\)) check_part "$cur"; case $stk in [oda]*) sub_end ;; *) cur= ;; esac ;;
      o\#)
        case $lastc in
          '' | [\ \	\;\&\|\(\)] | "$nl") case $cs in *"$nl"*) cs=$nl${cs#*"$nl"} ;; *) cs= ;; esac ;;
          *) cur=$cur$ch ;;
        esac ;;
      o\<)
        cur=$cur$ch
        case $lang$cs in
          sh\<\<*) cur=$cur'<<' cs=${cs#??} ;; # <<<, a here-string
          sh\<*) cs=${cs#?}; cur=$cur'<'; case $stk in a*) ;; *) heredoc_start ;; esac ;;
        esac ;;
      o"$nl") check_part "$cur"; cur=; heredoc_skip ;;
      *) check_part "$cur"; cur= ;;
    esac
    lastc=$ch
  done
  check_part "$cur"
  while [ -n "$stk" ]; do sub_end; check_part "$cur"; done
  return 0
}
# heredoc_start: after <<, queues the heredoc's word, if it has one: u or q (unquoted, quoted), - or _ (<<- or <<),
# then the word without its quotes.
heredoc_start() {
  # A heredoc's word starts with a letter, _, a quote or a backslash: $((1<<3)) is arithmetic.
  hw=${cs#-}
  hw=${hw#"${hw%%[!\ \	]*}"}
  case $hw in [A-Za-z_\'\"\\]*) ;; *) return 0 ;; esac
  hd=_
  case $cs in -*) hd=- cs=${cs#-} cur=$cur- ;; esac
  hb=${cs%%[!\ \	]*}
  cs=${cs#"$hb"}
  hw=${cs%%[\ \	\;\|\&\<\>\(\)$nl]*}
  cs=${cs#"$hw"} cur=$cur$hb$hw hq=u
  while :; do
    case $hw in *[\'\"\\]*) hq=q hw=${hw%%[\'\"\\]*}${hw#*[\'\"\\]} ;; *) break ;; esac
  done
  [ -z "$hw" ] || hdq=$hdq$hq$hd$hw$nl
}
# heredoc_skip: at a newline, takes the queued heredocs' bodies off the text. <<- ends at the word
# after blanks: a JSON tab arrives as a space.
heredoc_skip() {
  [ -n "$hdq" ] || return 0
  hrest=
  # read takes a line in its own length, where cutting one off a long text costs the whole text
  # in bash. The heredoc adds one newline at the end, taken off again below. A heredoc the shell
  # cannot write is a failure of the hook: the rest of the text would go unread.
  [ -d "${TMPDIR:-/tmp}" ] && {
    while [ -n "$hdq" ]; do
      he=${hdq%%"$nl"*} hdq=${hdq#*"$nl"}
      hq=${he%"${he#?}"} he=${he#?}
      hd=${he%"${he#?}"} he=${he#?}
      hbody=
      while IFS= read -r hl; do
        [ "$hd" = - ] && hl=${hl#"${hl%%[!\ \	]*}"}
        [ "$hl" = "$he" ] && break
        [ "$hq" = q ] || hbody=$hbody$hl$nl
      done
      case $hq$hbody in u*'$('* | u*'`'*) (check_cmd "$hbody") || exit $? ;; esac
    done
    while IFS= read -r hl; do hrest=$hrest$hl$nl; done
  } <<PUSH_GUARD_TEXT || exit 3
$cs
PUSH_GUARD_TEXT
  cs=${hrest%"$nl"}
}
# sub_start MODE: a substitution or a group begins; MODE (o, d, or O, D for backticks) is the mode to
# return to, and the command so far is set aside. MODE a is arithmetic, $(( or ((: back to o at its
# end, and a << inside it is a shift, not a heredoc.
sub_start() {
  stk=$1$cur$soh$stk cur= md=o
}
# sub_end: back to the command around it.
sub_end() {
  top=${stk%%"$soh"*} stk=${stk#*"$soh"}
  cur=${top#?}
  case $top in [oOa]*) md=o ;; *) md=d ;; esac
}

# check_part TEXT: one simple command, in words. Wrappers (env, sudo, xargs, Start-Process...) are
# taken off to reach the program; the code a shell or eval is given (bash -c, powershell -Command,
# cmd /c, Invoke-Expression...) is checked as commands.
check_part() {
  words "$1"
  IFS=$soh
  set -- $wl
  IFS=' '
  fromstdin=0 gopts=
  while [ $# -gt 0 ]; do
    case $1 in
      '') shift; continue ;;
      '$'*)
        # PowerShell: $x = <command> runs the command.
        case ${2-} in
          =) shift 2; continue ;;
          =*) w=${2#=}; shift 2; set -- "$w" "$@"; continue ;;
        esac
        case $1 in *=?*) w=${1#*=}; shift; set -- "$w" "$@"; continue ;; *=) shift; continue ;; esac
        # A variable names the program: refuse when what follows reads like one of ours.
        for t; do
          lower "$t"
          case $lc in push | tag | release | merge) refuse "a variable names the program in: $*" ;; esac
        done
        return 0 ;;
      *=*) shift; continue ;; # NAME=value before a command
    esac
    prog "$1"
    case $w in
      then | do | else | elif | if | while | until) shift; continue ;; # a keyword before a command
      env | sudo | doas | command | builtin | exec | nohup | time | nice | noglob | ! | timeout | xargs | \
        eval | iex | invoke-expression | start-process | saps | start)
        [ "$w" = xargs ] && fromstdin=1
        shift
        case $w in
          eval | iex | invoke-expression)
            case "$*" in *'$'*) mentions "$*$cmd" && refuse "$w runs text the hook cannot see: $*" ;; esac ;;
          start-process | saps | start)
            # -ArgumentList 'push origin main' is split as the program will see it.
            t="$*"; IFS=" 	,"; set -- $t; IFS=' ' ;;
        esac
        while [ $# -gt 0 ]; do
          case $1 in
            -u | -g | -C | -n | -L | -P | -d | -E | -s | -a | -k | -I) shift; [ $# -gt 0 ] && shift ;;
            '' | -*) shift ;;
            *) break ;;
          esac
        done
        [ "$w" = timeout ] && [ $# -gt 0 ] && shift # the duration
        case $w in
          eval) (use_lang sh; check_cmd "$*") || exit $?; return 0 ;;
          iex | invoke-expression) (use_lang ps; check_cmd "$*") || exit $?; return 0 ;;
        esac
        continue ;;
      bash | sh | zsh | dash | ksh | ash | mksh | powershell | pwsh | cmd)
        sh=$w
        shift
        while [ $# -gt 0 ]; do
          lower "$1"
          case $sh:$lc in
            powershell:-e | powershell:-ec | powershell:-en* | pwsh:-e | pwsh:-ec | pwsh:-en*)
              refuse "an encoded PowerShell command cannot be read" ;;
            powershell:-f | powershell:-file | pwsh:-f | pwsh:-file) return 0 ;; # a script file: not read
            *:-o | *:-O | *:--rcfile | *:--init-file | *:-executionpolicy | *:-ex | *:-ep | *:-windowstyle | \
              *:-w | *:-workingdirectory | *:-wd | *:-outputformat | *:-of | *:-inputformat | *:-if | \
              *:-configurationname)
              shift; [ $# -gt 0 ] && shift ;;
            *: | *:-* | *:/?) shift ;; # cmd's /c, /k, /s
            *) break ;;
          esac
        done
        [ $# -gt 0 ] || return 0
        case $lc in
          *[\ \	\;\&\|$nl]*) ;;
          *.sh | *.bash | *.ps1 | *.bat | *.cmd) return 0 ;; # a script file: not read
        esac
        case $sh in powershell | pwsh) l=ps ;; cmd) l=cmd ;; *) l=sh ;; esac
        (use_lang $l; check_cmd "$*") || exit $?
        return 0 ;;
    esac
    break
  done
  [ $# -gt 0 ] || return 0
  case $w in
    git) shift; check_git "$@"; return ;;
    gh) shift; check_gh "$@"; return ;;
    cd | chdir | pushd | set-location | sl | push-location) shift; change_dir "$@"; return ;;
  esac
  # A path to git or gh further on is the program, as in & "C:\Program Files\Git\cmd\git.exe" push.
  while [ $# -gt 0 ]; do
    case $1 in
      */[Gg][Ii][Tt] | */[Gg][Ii][Tt].[Ee][Xx][Ee] | *\\[Gg][Ii][Tt] | *\\[Gg][Ii][Tt].[Ee][Xx][Ee])
        shift; check_git "$@"; return ;;
      */[Gg][Hh] | */[Gg][Hh].[Ee][Xx][Ee] | *\\[Gg][Hh] | *\\[Gg][Hh].[Ee][Xx][Ee])
        shift; check_gh "$@"; return ;;
    esac
    shift
  done
}

# change_dir ARGS: follow cd, so a later bare git push is resolved where it runs.
change_dir() {
  while [ $# -gt 0 ]; do case $1 in -) break ;; '' | -*) shift ;; *) break ;; esac; done
  case ${1-} in
    '') dir=$HOME ;;
    -) dir= ;;
    /* | [A-Za-z]:* | \\*) dir=$1 ;;
    '~' | '~/'* | '~\'*) dir=$HOME${1#\~} ;;
    *) [ -n "$dir" ] && dir=$dir/$1 ;;
  esac
}

check_git() {
  while [ $# -gt 0 ]; do
    case $1 in
      -C | -c | --git-dir | --work-tree | --namespace | --config-env | --attr-source)
        [ $# -ge 2 ] || return 0
        gopts=$gopts$1$soh$2$soh
        shift 2 ;;
      --*=* | --bare) gopts=$gopts$1$soh; shift ;;
      '' | -*) shift ;;
      *) break ;;
    esac
  done
  [ $# -gt 0 ] || return 0
  lower "$1"
  sub=$lc
  shift
  case $sub in
    *'$'* | *%*) refuse "a variable names the git command" ;;
    push) check_push "$@" ;;
    tag) refuse "git tag: tags are the owner's" ;;
    # Built-in commands: git never lets an alias replace one.
    add | am | apply | bisect | blame | branch | checkout | cherry-pick | clean | clone | commit | config | \
      describe | diff | fetch | grep | init | log | ls-files | ls-remote | merge | merge-base | mv | pull | \
      rebase | reflog | remote | reset | restore | rev-list | rev-parse | revert | rm | show | show-ref | \
      stash | status | switch | symbolic-ref | update-ref | worktree | cat-file | for-each-ref | \
      format-patch | help | version | submodule | shortlog | ls-tree | check-ignore | gc | notes) ;;
    *) check_alias "$sub" "$@" ;;
  esac
}

# check_alias NAME ARGS: a git alias is checked as what it expands to.
check_alias() {
  a=$(git_in_dir config --get "alias.$1") && [ -n "$a" ] || return 0
  shift
  depth=$((depth + 1))
  [ "$depth" -le 5 ] || refuse "git aliases nest too deep to read"
  case $a in
    '!'*) (use_lang sh; check_cmd "${a#!} $*") || exit $? ;;
    *) (IFS=' 	'; set -- $a "$@"; check_git "$@") || exit $? ;;
  esac
}

check_push() {
  [ "$fromstdin" = 1 ] && refuse "git push under xargs takes arguments the hook cannot see"
  remote= haveremote= nrefs=0 delete= endopts= repo=
  while [ $# -gt 0 ]; do
    t=$1
    shift
    case $t in
      '') continue ;;
      *'>' | *'<') [ $# -gt 0 ] && shift; continue ;; # a redirection and its file
      *'>'* | *'<'*) continue ;;
      *'$'* | *%*) refuse "a variable in git push ($t) hides the destination" ;;
    esac
    if [ -z "$endopts" ]; then
      case $t in
        --) endopts=1; continue ;;
        --*)
          # git takes any unambiguous prefix of a long option.
          o=${t%%=*}
          for n in --force --force-with-lease --force-if-includes; do
            case $n in "$o"*) refuse "a force push ($t)" ;; esac
          done
          for n in --mirror --all --branches --tags --follow-tags; do
            case $n in "$o"*) refuse "git push $t pushes main or tags" ;; esac
          done
          case --repo in "$o"*) if [ ${#o} -ge 5 ]; then
            haveremote=1
            case $t in *=*) remote=${t#*=} ;; *) remote=${1-}; [ $# -gt 0 ] && shift ;; esac
          fi; continue ;; esac
          case --delete in "$o"*) [ ${#o} -ge 4 ] && delete=1; continue ;; esac
          case $t in *=*) continue ;; esac
          for n in --receive-pack --exec --push-option --recurse-submodules; do
            case $n in "$o"*) [ ${#o} -ge 4 ] && [ $# -gt 0 ] && shift; continue 2 ;; esac
          done
          continue ;;
        -?*)
          c=${t#-}
          case $c in *f*) refuse "a force push ($t)" ;; esac
          case $c in *d*) delete=1 ;; esac
          case $c in *o) [ $# -gt 0 ] && shift ;; esac # -o takes a value
          continue ;;
      esac
    fi
    if [ -z "$haveremote" ]; then
      haveremote=1 remote=$t
      continue
    fi
    nrefs=$((nrefs + 1))
    check_refspec "$t"
  done
  [ "$nrefs" -gt 0 ] || [ -n "$delete" ] || check_bare
}

# check_refspec REFSPEC: [+]<src>[:<dst>], or :<dst> to delete.
check_refspec() {
  r=$1
  case $r in
    +*) refuse "a force push ($r)" ;;
    *'*'*) refuse "a wildcard refspec ($r) can reach main or tags" ;;
  esac
  lower "$r"
  [ "$lc" = tag ] && refuse "git push ... tag <name> pushes a tag"
  [ -n "$repo" ] || git_in_dir rev-parse --git-dir >/dev/null ||
    refuse "cannot read the repository this git push runs in"
  repo=1
  if [ -n "$delete" ]; then
    check_deletion "$r"
    return
  fi
  case $r in
    :) refuse "git push <remote> : pushes every matching branch, main too" ;;
    :*) check_deletion "${r#:}"; return ;;
    *:*) src=${r%%:*} dst=${r#*:} ;;
    *) src=$r dst= ;;
  esac
  if [ -z "$dst" ]; then
    lower "$src"
    case $lc in
      head | @) src=$(current_branch) || refuse "cannot tell which branch HEAD is in git push $r" ;;
    esac
  fi
  full=$(git_in_dir rev-parse --symbolic-full-name "$src")
  lower "$src $full"
  case $lc in *refs/tags/*) refuse "git push $r pushes a tag" ;; esac
  # Without a destination, git pushes to the source's own full name.
  [ -n "$dst" ] || dst=${full:-$src}
  check_dst "$dst" "$r"
}
check_dst() {
  lower "$1"
  case ${lc#refs/heads/} in main) refuse "git push $2 reaches main" ;; esac
  case $lc in refs/tags/*) refuse "git push $2 writes a tag" ;; esac
}
# check_deletion REF: deleting is let through only for a branch the remote is known to have.
check_deletion() {
  check_dst "$1" ":$1"
  case $remote in '' | */* | *:* | *\\*) refuse "cannot tell what deleting $1 on $remote removes" ;; esac
  git_in_dir show-ref --verify --quiet "refs/remotes/$remote/${1#refs/heads/}" ||
    refuse "deleting $1: it is not a known branch of $remote"
}

# check_bare: a git push without a refspec goes where git's configuration says; ask git.
check_bare() {
  b=$(current_branch) || refuse "a bare git push from a detached or unreadable HEAD"
  lower "$b"
  [ "$lc" = main ] && refuse "git push from main pushes main"
  cfg=$(git_in_dir config --get-regexp '^push\.default$|^remote\..*\.(push|mirror)$')
  lower "$cfg"
  case "$nl$lc$nl" in *"${nl}push.default matching$nl"*) refuse "push.default=matching pushes main too" ;; esac
  case $lc in *.push\ *) refuse "a remote.<name>.push setting decides what a bare git push sends" ;; esac
  case "$lc$nl" in *".mirror true$nl"* | *".mirror yes$nl"* | *".mirror on$nl"* | *".mirror 1$nl"*)
    refuse "a mirror remote pushes every ref" ;;
  esac
  p=$(git_in_dir rev-parse --abbrev-ref --symbolic-full-name '@{push}') || return 0
  lower "$p"
  case $lc in */main) refuse "git push from $b goes to $p" ;; esac
  return 0
}

check_gh() {
  while [ $# -gt 0 ]; do
    case $1 in
      -R | --repo) shift; [ $# -gt 0 ] && shift ;;
      '' | -*) shift ;;
      *) break ;;
    esac
  done
  [ $# -gt 0 ] || return 0
  lower "$1"
  sub=$lc
  shift
  case $sub in
    *'$'* | *%*) refuse "a variable names the gh command" ;;
    release) refuse "gh release: releases are the owner's" ;;
    pr)
      while [ $# -gt 0 ]; do
        case $1 in -R | --repo) shift; [ $# -gt 0 ] && shift ;; '' | -*) shift ;; *) break ;; esac
      done
      lower "${1-}"
      [ "$lc" = merge ] || return 0
      [ "$fromstdin" = 1 ] && refuse "gh pr merge under xargs takes arguments the hook cannot see"
      for t; do
        case $t in --auto | --auto=* | --admin | --admin=*) refuse "gh pr merge $t: merge through the PR by hand" ;; esac
      done ;;
    api) check_api "$@" ;;
  esac
}

# check_api ARGS: a call that writes (-X/--method other than GET, or fields, which make it a POST)
# on a release or a git ref endpoint, or on one the hook cannot see, is refused. The endpoint is the
# first argument that is not an option or an option's value.
check_api() {
  method= fields= ep= target= hidden=
  while [ $# -gt 0 ]; do
    t=$1
    shift
    case $t in
      -X | --method) method=${1-}; [ $# -gt 0 ] && shift ;;
      -X*) method=${t#-X} ;;
      --method=*) method=${t#*=} ;;
      -f | -F | --field | --raw-field | --input) fields=1; [ $# -gt 0 ] && shift ;;
      -f* | -F* | --field=* | --raw-field=* | --input=*) fields=1 ;;
      -H | --header | -q | --jq | -t | --template | -p | --preview | --hostname | --cache)
        [ $# -gt 0 ] && shift ;;
      -*) ;;
      *) [ -n "$ep" ] || ep=$t ;;
    esac
  done
  case $ep in *'$'* | *%*) hidden=1 ;; esac
  lower "$ep"
  case $lc in *releases* | *git/refs*) target=1 ;; esac
  lower "$method"
  m=$lc
  if [ -z "$m" ]; then
    if [ -n "$fields" ]; then m=post; else m=get; fi
  fi
  case $m in get | head) return 0 ;; esac
  [ "$fromstdin" = 1 ] && refuse "gh api under xargs takes arguments the hook cannot see"
  [ -n "$target$hidden" ] && refuse "gh api $m on a release or a git ref"
  return 0
}

main() {
  depth=0 fromstdin=0 gopts=
  if ! json_string command; then
    mentions "$input" && refuse "the hook could not read this command, and it mentions a push, a tag, a release, a merge or the API"
    exit 0
  fi
  cmd=$val
  # The decoding below must give back what the shell will see. It reads a \u escape other than
  # & < > ' as a space, and a backspace or form feed too, while the shell keeps the character in a
  # word: a # after it is no comment, and escapes can spell a guarded word. Bash also keeps a CR in
  # a word, where PowerShell ends a line. Claude Code escapes only rare control characters this
  # way, so each such escape is refused; in the cwd, every escape but a quote or a slash is.
  # Escaped backslashes are set aside first. A raw separator byte, which no valid JSON holds,
  # would decode as a backslash: refuse it too.
  case $input in *"$soh"*) refuse "the input holds a raw control character" ;; esac
  use_lang sh
  json_string tool_name && [ "$val" = PowerShell ] && use_lang ps
  cwd=
  json_string cwd && cwd=$val
  case $cmd$cwd in
    *\\*)
      rest=$(printf '%s' "$cmd" | sed -e 's/\\\\//g' -e 's/\\u0026//g' -e 's/\\u0027//g' -e 's/\\u003[cCeE]//g')
      case $rest in *\\[ubf]*) refuse "the command holds an escape the hook cannot read faithfully" ;; esac
      case $lang$rest in sh*\\r*) refuse "the command holds a carriage return, which Bash keeps in a word" ;; esac
      rest=$(printf '%s' "$cwd" | sed -e 's/\\\\//g' -e 's|\\["/]||g')
      case $rest in *\\*) refuse "the cwd holds an escape the hook cannot read faithfully" ;; esac
      ;;
  esac
  case $cwd$cmd in
    *\\*)
      # Both through one sed; the x keeps a trailing empty line.
      out=$(printf '%s\n%s\n' "$cwd" "$cmd" | unescape; printf x)
      out=${out%x}
      out=${out%"$nl"}
      dir=${out%%"$nl"*} cmd=${out#*"$nl"} ;;
    *) dir=$cwd ;;
  esac
  [ -n "$dir" ] || dir=$PWD
  check_cmd "$cmd"
  exit 0
}

# Nothing guarded can run without one of these words: the common path ends here, before any other
# process starts.
if json_string command; then
  case $val in
    *[Gg][Ii][Tt]* | *[Gg][Hh]* | *[Pp][Uu][Ss][Hh]* | *[Tt][Aa][Gg]* | *[Rr][Ee][Ll][Ee][Aa][Ss][Ee]* | \
      *[Mm][Ee][Rr][Gg][Ee]* | *[Pp][Ww][Ss][Hh]* | *[Pp][Oo][Ww][Ee][Rr][Ss][Hh][Ee][Ll][Ll]* | *\\u*) ;;
    *) exit 0 ;;
  esac
fi
# The word separator is \037, not \001: bash 3.2, macOS's sh, uses \001 internally and drops it
# from IFS and expansions, so words never split and a git push went unseen.
soh=$(printf '\037')
# A failure of the hook itself fails closed for the commands it guards, open for the rest.
(main)
status=$?
case $status in 0 | 2) exit "$status" ;; esac
mentions "$input" || exit 0
printf 'push-guard: the hook failed (status %s) on a command that mentions a push, a tag, a release, a merge or the API. Hand over the command.\n' "$status" >&2
exit 2
