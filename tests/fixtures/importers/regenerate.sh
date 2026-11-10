#!/bin/sh
# Regenerate the CMake export and CPS fixtures of buildlog-cpp's importers
# from the project in src/greet: prefix/ is a relocatable install (staged
# with DESTDIR under the configured prefix /opt/greet, which only the
# absolute-destination export records), buildtree/ holds the export() files
# of the build tree. The build tree's absolute paths are rewritten to the
# fixed roots /work/greet-src and /work/greet-build so the checked-in files
# do not depend on this checkout (the binaries' debug info gets the same
# roots through -ffile-prefix-map and -oso_prefix); nothing else is edited.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
work="$(cd "$here/../.." && pwd)/.work/importers"
rm -rf "$work" "$here/prefix" "$here/buildtree"
mkdir -p "$work"
cmake -S "$here/src/greet" -B "$work/build" -G "Ninja Multi-Config" \
  -DCMAKE_INSTALL_PREFIX=/opt/greet -DCMAKE_OSX_ARCHITECTURES=arm64 -DCMAKE_OSX_DEPLOYMENT_TARGET=13.0 \
  "-DCMAKE_CXX_FLAGS=-ffile-prefix-map=$here/src/greet=/work/greet-src -ffile-prefix-map=$work/build=/work/greet-build" \
  "-DCMAKE_EXE_LINKER_FLAGS=-Wl,-oso_prefix,$work/build/" \
  "-DCMAKE_SHARED_LINKER_FLAGS=-Wl,-oso_prefix,$work/build/" \
  >/dev/null
for config in Release Debug; do
  cmake --build "$work/build" --config "$config" >/dev/null
  DESTDIR="$work/stage" cmake --install "$work/build" --config "$config" >/dev/null
done
cp -R "$work/stage/opt/greet" "$here/prefix"
mkdir -p "$here/buildtree"
for f in "$work/build"/GreetBuild*-targets.cmake; do
  sed -e "s|$work/build|/work/greet-build|g" -e "s|$here/src/greet|/work/greet-src|g" \
    "$f" >"$here/buildtree/$(basename "$f")"
done
cmake --version | head -1 >"$here/CMAKE_VERSION"
