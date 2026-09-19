// Temporary: builds a development macOS player for the artel-mcp prototype. Not committed.
//
// TitleScene carries an ArtelManager whose serialized Server points at stage. A scene-placed
// manager wins over -artel-server (see ArtelManager.SpawnInDevelopmentBuilds), so for the local
// build we strip it from the build output only; the SDK then spawns its own manager and reads
// the launch arguments. The scene on disk is untouched.
using System.Collections.Generic;
using UnityEditor;
using UnityEditor.Build;
using UnityEditor.Build.Reporting;
using UnityEngine;
using UnityEngine.SceneManagement;

public static class ArtelMcpBuild
{
    public static void Mac()
    {
        var paths = new List<string>();
        foreach (var s in EditorBuildSettings.scenes) if (s.enabled) paths.Add(s.path);
        var output = System.Environment.GetEnvironmentVariable("ARTEL_BUILD_OUT");
        if (string.IsNullOrEmpty(output)) output = "/tmp/WordVenture.app";
        var report = BuildPipeline.BuildPlayer(new BuildPlayerOptions
        {
            scenes = paths.ToArray(),
            locationPathName = output,
            target = BuildTarget.StandaloneOSX,
            options = BuildOptions.Development | BuildOptions.CleanBuildCache,
        });
        Debug.Log("[ArtelMcpBuild] " + report.summary.result + " " + report.summary.totalSize + " bytes -> " + output);
        if (report.summary.result != BuildResult.Succeeded) EditorApplication.Exit(1);
    }
}

public sealed class ArtelMcpStripSceneManager : IProcessSceneWithReport
{
    public int callbackOrder => 0;

    public void OnProcessScene(Scene scene, BuildReport report)
    {
        if (report == null) return; // editor play mode: leave the scene alone
        if (System.Environment.GetEnvironmentVariable("ARTEL_MCP_STRIP_MANAGER") != "1") return;
        var type = System.Type.GetType("Artel.ArtelManager, Artel.Runtime");
        if (type == null) { Debug.LogWarning("[ArtelMcpBuild] Artel.ArtelManager type not found"); return; }
        var removed = 0;
        foreach (var root in scene.GetRootGameObjects())
            foreach (var c in root.GetComponentsInChildren(type, true))
            {
                Object.DestroyImmediate(c, true);
                removed++;
            }
        if (removed > 0) Debug.Log("[ArtelMcpBuild] stripped " + removed + " ArtelManager from " + scene.name);
    }
}
