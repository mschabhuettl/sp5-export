#!/bin/sh
# Self-test of scripts/check-repo.sh for the private host and account patterns: a scratch repository with a
# clean commit (author identity on a private domain) must pass; a planted LAN address in a file and a planted
# account name in a commit message must fail. The planted words are assembled at run time.
set -eu
ROOT=$(git rev-parse --show-toplevel)
WORK=$(mktemp -d)
trap 'rm -rf "${WORK:?}"' EXIT
account="schab""huettl"
lan="192.""168.0.5"

git init -q "$WORK/repo"
cd "$WORK/repo"
git config user.name "Owner"
git config user.email "owner@$account.example"
mkdir scripts
cp "$ROOT/scripts/check-repo.sh" scripts/
git add scripts && git commit -q -m "Clean commit"
fail=0
if sh scripts/check-repo.sh >/dev/null 2>&1; then echo "clean repository passes"; else echo "clean repository fails"; fail=1; fi

echo "host = $lan" > config.txt
git add config.txt && git commit -q -m "Add config"
if sh scripts/check-repo.sh >/dev/null 2>&1; then echo "not detected: LAN address"; fail=1; else echo "detected: LAN address"; fi
git rm -q config.txt && git commit -q -m "Remove config"

git commit -q --allow-empty -m "Notes for $account"
if sh scripts/check-repo.sh >/dev/null 2>&1; then echo "not detected: account name"; fail=1; else echo "detected: account name"; fi

[ "$fail" -eq 0 ] && echo "test-check-repo: OK" || { echo "test-check-repo: FAILED"; exit 1; }
