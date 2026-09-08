#!/usr/bin/env bash
# Refuse to publish anything that names a credential or an internal environment.
# Runs over the files git would commit. Non-zero exit on the first class of hit.
set -uo pipefail
cd "$(dirname "$0")"
files=$(git add -A -n 2>/dev/null | sed "s/^add '//; s/'$//")
[ -n "$files" ] || { echo "nothing tracked"; exit 0; }
fail=0
check() { # label, regex
  hits=$(grep -nIE "$2" $files 2>/dev/null | grep -v "^audit_public.sh:" | grep -v "github.com/pawan-bobbili-ntnx/nkp-dev-e2e" | head -5)
  if [ -n "$hits" ]; then echo "FAIL $1:"; echo "$hits" | cut -c1-140 | sed 's/^/  /'; fail=1; else echo "ok   $1"; fi
}
check "tokens"           'ghp_[A-Za-z0-9]{20,}|gh[ops]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]+|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----'
check "passwords"        '(PASSWORD|Password|password)[=:] *["'"'"']?[A-Za-z0-9@#%^&*_+-]{12,}'
check "lab hostnames"    'pc\.dev\.nkp\.sh|\.nkp\.sh\b|ncn-dev-sandbox|vlan170'
check "lab addresses"    '\b10\.22\.[0-9]+\.[0-9]+\b|\b10\.138\.[0-9]+\.[0-9]+\b'
check "home paths"       '/Users/[a-z.]+/|/home/(admin|ubuntu|[a-z]+\.[a-z]+)/'
check "personal handles" 'pawan-bobbili|pawanbob|pawan\.bobbili'
exit $fail
