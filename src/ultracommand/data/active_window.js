// Loaded by Ultra-Command into KWin: reports the active window and the list of
// windows, so Ultra-Command knows where it types (terminal or not), can switch
// windows by voice and can tell the assistant what is open.
const SERVICE = "io.github.huppiflupp.UltraCommand";
const PATH = "/io/github/huppiflupp/UltraCommand";

function report() {
    const list = [];
    for (const w of workspace.windowList()) {
        if (!w.normalWindow || w.skipTaskbar) continue;
        list.push({id: w.internalId.toString(), cls: String(w.resourceClass || ""),
                   caption: String(w.caption || ""), active: w === workspace.activeWindow});
    }
    callDBus(SERVICE, PATH, SERVICE, "Windows", JSON.stringify(list));
}

// Titles can change several times a second (progress bars, clocks): report
// those at most every 2 s. Activation, new and closed windows report at once.
let lastCaptionReport = 0;
function captionChanged() {
    const now = Date.now();
    if (now - lastCaptionReport < 2000) return;
    lastCaptionReport = now;
    report();
}

function watch(w) {
    if (w) w.captionChanged.connect(captionChanged);
}

workspace.windowActivated.connect(report);
workspace.windowAdded.connect(function (w) { watch(w); report(); });
workspace.windowRemoved.connect(report);
workspace.windowList().forEach(watch);
report();
