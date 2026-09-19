#!/usr/bin/env bash
# Builds the sample game (artel-sdk/samples/WordVenture) as a development macOS player that
# takes -artel-server from the command line.
#
# TitleScene carries an ArtelManager whose serialized Server points at stage, and a scene-placed
# manager wins over -artel-server. ArtelMcpBuild.cs strips it from the build output only
# (IProcessSceneWithReport); the scene on disk is untouched. The script is copied into the
# project for the build and removed afterwards, so the sample repo stays clean.
#
#   scripts/build-wordventure.sh [output.app]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PROJECT="${WORDVENTURE_PROJECT:-$HERE/../../artel-sdk/samples/WordVenture}"
OUT="${1:-$HERE/../../builds/WordVenture-dev.app}"
UNITY="${UNITY:-/Applications/Unity/Hub/Editor/$(grep m_EditorVersion "$PROJECT/ProjectSettings/ProjectVersion.txt" | awk '{print $2}')/Unity.app/Contents/MacOS/Unity}"
mkdir -p "$PROJECT/Assets/Editor" "$(dirname "$OUT")"
cp "$HERE/ArtelMcpBuild.cs" "$PROJECT/Assets/Editor/ArtelMcpBuild.cs"
cleanup() { rm -f "$PROJECT/Assets/Editor/ArtelMcpBuild.cs" "$PROJECT/Assets/Editor/ArtelMcpBuild.cs.meta"; }
trap cleanup EXIT
ARTEL_MCP_STRIP_MANAGER=1 ARTEL_BUILD_OUT="$OUT" "$UNITY" -batchmode -quit -nographics \
  -projectPath "$PROJECT" -buildTarget OSXUniversal -executeMethod ArtelMcpBuild.Mac -logFile /tmp/wv-build.log
grep -E "\[ArtelMcpBuild\]" /tmp/wv-build.log
echo "built: $OUT"
