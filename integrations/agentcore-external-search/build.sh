#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
DESTINATION=${1:?Usage: sh build.sh /absolute/new/bundle-directory}
case "$DESTINATION" in
    /*) ;;
    *) printf '%s\n' 'Bundle path must be absolute' >&2; exit 1 ;;
esac
DESTINATION=$("${PYTHON:-python3}" -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$DESTINATION")
REPOSITORY=$(git -C "$ROOT" rev-parse --show-toplevel)
case "$DESTINATION/" in
    "$REPOSITORY/"*) printf '%s\n' 'Build outside the checkout' >&2; exit 1 ;;
esac
test ! -e "$DESTINATION" || { printf '%s\n' 'Use a new bundle directory' >&2; exit 1; }
git -C "$ROOT" diff --quiet HEAD -- . || { printf '%s\n' 'Commit integration changes before building' >&2; exit 1; }
test -z "$(git -C "$ROOT" ls-files --others --exclude-standard .)" || { printf '%s\n' 'Commit new integration files before building' >&2; exit 1; }
mkdir -p "$DESTINATION"
cp "$ROOT/requirements.txt" "$DESTINATION/requirements.txt"
if [ -n "${UV:-}" ]; then
    "$UV" pip install --python-version 3.12 --python-platform aarch64-manylinux_2_28 \
        --only-binary=:all: --require-hashes --target "$DESTINATION" -r "$DESTINATION/requirements.txt"
else
    "${PYTHON:-python3}" -m pip install --no-compile --only-binary=:all: --require-hashes \
        --python-version 3.12 --implementation cp --abi cp312 \
        --platform manylinux_2_28_aarch64 --platform manylinux2014_aarch64 \
        --target "$DESTINATION" -r "$DESTINATION/requirements.txt"
fi
for DIRECTORY in runtime provisioner; do
    mkdir "$DESTINATION/$DIRECTORY"
    cp "$ROOT/$DIRECTORY/"*.py "$DESTINATION/$DIRECTORY/"
done
git -C "$ROOT" rev-parse HEAD > "$DESTINATION/source-commit.txt"
(
    cd "$DESTINATION"
    { find runtime provisioner -type f -name '*.py' -print; printf '%s\n' requirements.txt source-commit.txt; } \
        | LC_ALL=C sort | xargs sha256sum
) > "$DESTINATION/source-sha256.txt"
chmod -R a-w "$DESTINATION"
printf '%s\n' "$DESTINATION"
