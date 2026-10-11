#!/bin/sh
set -eu
revision="$1"
variant="$2"
test "$(go version)" = 'go version go1.21.3 linux/amd64'
mkdir /efsn
cp -a /source/. /efsn/
cd /efsn
rm -rf /efsn/.git
sha256sum go.mod go.sum > /tmp/source-dependencies.sha256
go mod verify
if [ "$variant" != upstream ]; then
    go test -race -count=1 -timeout=180s ./tests/tracing
fi
if [ "$variant" = gateway ]; then
    go test ./cmd/utils -run '^TestExplicitEmptyBootstrapNodes$' -count=1
fi
go run build/ci.go install -git-commit "$revision" ./cmd/efsn
sha256sum --check /tmp/source-dependencies.sha256
cp build/bin/efsn /output/efsn
