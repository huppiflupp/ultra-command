// Loaded by Ultra-Command into KWin: reports the active window, so ultra-command knows
// where it types (terminal or not) and can tell the assistant.
function report(w) {
    if (!w) return;
    callDBus("io.github.huppiflupp.UltraCommand", "/io/github/huppiflupp/UltraCommand",
             "io.github.huppiflupp.UltraCommand", "ActiveWindow",
             String(w.resourceClass || ""), String(w.caption || ""));
}
workspace.windowActivated.connect(report);
report(workspace.activeWindow);
