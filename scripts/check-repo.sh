#!/bin/sh
# Pre-publish check of the repository: no data files, no unwanted words in commits, file names or contents
# (tracked files including binaries, and every commit message).
# The patterns use character classes so that this script does not match itself.
set -u
export LC_ALL=C.UTF-8
cd "$(git rev-parse --show-toplevel)" || exit 2
status=0

fail() {
    echo "check-repo: $1" >&2
    status=1
}

# Case-insensitive patterns.
CI_AI='[c]laude|[a]nthropic|[c]hatgpt|[o]penai|[l]lm|[c]odex'
CI_ORG='[s]hiftfold|[S]teiermark|[R]ote[sn]? [K]reuz|[R]ed [C]ross|[r]oteskreuz|[r]ls-dienstplanung|[S]tra(ß|ss)gang|\b[G]raz\b'
CI_FONT='[S]ource[ _-]?[S]ans|[D]unant'
# Case-sensitive patterns (short names of the organisation, word boundaries against false hits such as URLs).
CS='\b[R]LS\b|\b[Ö]RK\b|\b[N]-CT\b|\b[K]-CT\b|\b[G]/GU\b|\b[O]vD\b|\b[Ä]ND\b|\b[C]T1450|\b[1]450\b|\b[E]CN\b|\b[F]-8\b|\b[S]-10\b'
# The demo world is invented: no area ids, shift codes or area names of a real dispatch centre.
# Codes: whole words, case sensitive, text files only (binary files are left to check-images.sh).
CODES='\b([N]CT|[K]CTS?|[A]END|[N]EM|[O]vD|[O]VD|[D]P|[N]-CT|[K]-CT)\b|(^|[^[:alnum:]])[Ä]ND\b'
# Area names: case-insensitive phrases.
CI_AREAS='[N]otruf [C]alltaking|[K]rankentransport [C]alltaking|[O]ffizier vom [D]ienst|[Ä]rztenotdienst|[N]otarzt-[E]insatzmittel'
# The owner's private domain, account name and LAN addresses (case-insensitive). Commit messages only: the
# author and committer identity is the owner's own and may contain the domain.
CI_PRIVATE='[s]chabhuettl|\b192\.168\.[0-9]'

files=$(git ls-files | grep -i -E '\.(db[f]|cd[x]|fp[t]|ba[k]|zi[p]|xls[x]|cs[v])$')
[ -n "$files" ] && fail "data files tracked:
$files"

log=$(git log --all --format='%an %ae %cn %ce%n%B' 2>/dev/null)
echo "$log" | grep -q -i -E "$CI_AI|[c]o-authored|[g]enerated with|\b[a]i\b" \
    && fail "commit metadata or messages contain an unwanted word"
echo "$log" | grep -q -i -E "$CI_ORG|$CI_FONT" && fail "commit metadata or messages contain an organisation or font name"
echo "$log" | grep -q -E "$CS" && fail "commit metadata or messages contain an organisation short name"
echo "$log" | grep -q -E "$CODES" && fail "commit metadata or messages contain a real area or shift code"
echo "$log" | grep -q -i -E "$CI_AREAS" && fail "commit metadata or messages contain a real area name"
git log --all --format='%B' | grep -q -i -E "$CI_PRIVATE" && fail "commit messages contain a private host or account"

names=$(git ls-files | grep -i -E '[b]rief|[c]laude|[a]gents\.md|\.[c]laude')
[ -n "$names" ] && fail "unwanted file names tracked:
$names"
names=$(git ls-files | grep -v '^\.[g]ithub/' | grep -i -E "$CI_ORG|$CI_FONT"; git ls-files | grep -E "$CS")
[ -n "$names" ] && fail "unwanted names in file names:
$names"

# git grep -a also searches binary files.
hits=$(git grep -a -l -i -E "$CI_AI")
[ -n "$hits" ] && fail "unwanted words in tracked files:
$hits"
hits=$(git grep -a -l -i -E "$CI_ORG|$CI_FONT")
[ -n "$hits" ] && fail "organisation, place or font name in tracked files:
$hits"
hits=$(git grep -a -l -E "$CS")
[ -n "$hits" ] && fail "organisation short name in tracked files:
$hits"
hits=$(git grep -I -l -E "$CODES")
[ -n "$hits" ] && fail "real area or shift code in tracked files:
$hits"
hits=$(git grep -a -l -i -E "$CI_AREAS")
[ -n "$hits" ] && fail "real area name in tracked files:
$hits"
hits=$(git grep -a -l -i -E "$CI_PRIVATE")
[ -n "$hits" ] && fail "private host or account in tracked files:
$hits"
names=$(git ls-files | grep -i -E "$CI_PRIVATE")
[ -n "$names" ] && fail "private host or account in file names:
$names"

[ "$status" -eq 0 ] && echo "check-repo: ok"
exit "$status"
