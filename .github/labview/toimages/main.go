// Command lvci-toimages-runner renders position-aware "frames" JSON for the
// in-place VI Browser. It is a ONE-SHOT batch tool: it renders every VI in
// a worklist by shelling out to `lvctl toimages`, writes <blob>.json into the
// by-blob store, then exits.
//
// The render engine is R&D's lvctl `toimages` (native VI Server TCP client +
// Go-side PNG encoding — no lv_listener, no wsapi). lvctl owns the LabVIEW
// lifecycle: its first invocation launches LabVIEW (with -pref $LABVIEW_CONF,
// which enables VI Server TCP on port 3363 and suppresses dialogs), waits for
// the handshake, and leaves it running so later invocations just attach.
//
// It implements exactly the contract that the PUBLIC workflow
// (.github/workflows/vi-snapshots-json.yml in the consumer repo) drives:
//
//	env WORKSPACE   = repo worktree root (read-only)             default /work
//	env WORKLIST    = TSV of "<blob>\t<relpath>" lines           default /out/worklist.tsv
//	env OUT_BY_BLOB = output dir; writes <blob[:2]>/<blob>.json   default /out/by-blob
//
// Render-engine wiring (defaults suit the image's Dockerfile ENV):
//
//	env LVCTL          path to the lvctl binary                  default /app/lvctl
//	env LABVIEW_PATH    LabVIEW install dir (read by lvctl)       default /usr/local/natinst/LabVIEW-2026-64
//	env LABVIEW_CONF    labview.conf for -pref (read by lvctl)    default /app/labview.conf
//	env RENDER_TIMEOUT  per-VI lvctl timeout                      default 5m
package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func main() {
	if err := run(); err != nil {
		slog.Error("toimages runner failed", "error", err)
		os.Exit(1)
	}
}

func run() error {
	workspace := env("WORKSPACE", "/work")
	worklistPath := env("WORKLIST", "/out/worklist.tsv")
	outByBlob := env("OUT_BY_BLOB", "/out/by-blob")
	lvctlPath := env("LVCTL", "/app/lvctl")
	renderTimeout := 5 * time.Minute
	if d := os.Getenv("RENDER_TIMEOUT"); d != "" {
		if parsed, perr := time.ParseDuration(d); perr == nil {
			renderTimeout = parsed
		}
	}

	if _, err := exec.LookPath(lvctlPath); err != nil {
		return fmt.Errorf("lvctl not found at %q: %w", lvctlPath, err)
	}

	// LabVIEW itself is launched lazily by lvctl on the first conversion (with
	// -pref $LABVIEW_CONF so VI Server TCP is enabled), then reused for the rest.
	// Xvfb is already up (docker-entrypoint.sh) so front-panel capture has a
	// display.

	// Load the worklist.
	items, err := readWorklist(worklistPath)
	if err != nil {
		return fmt.Errorf("read worklist %q: %w", worklistPath, err)
	}
	slog.Info("worklist loaded", "count", len(items))

	// 4. Render each VI. Best-effort: a failure is logged and skipped so one bad
	//    VI never sinks the batch (that VI just stays on the 1.0 flat view).
	ok, failed := 0, 0
	for i, it := range items {
		viPath := filepath.Join(workspace, it.rel)
		jsonStr, err := convertOne(lvctlPath, viPath, renderTimeout)
		if err != nil {
			slog.Warn("convert failed (skipped)", "vi", it.rel, "error", err)
			failed++
			continue
		}
		if err := writeJSON(outByBlob, it.blob, jsonStr); err != nil {
			slog.Warn("write failed (skipped)", "vi", it.rel, "error", err)
			failed++
			continue
		}
		ok++
		slog.Info("rendered", "progress", fmt.Sprintf("%d/%d", i+1, len(items)), "vi", it.rel, "bytes", len(jsonStr))
	}

	// A non-empty worklist that rendered nothing is a systemic failure (e.g. VI
	// scripting / headless capture is broken), not just a few bad VIs.
	if ok == 0 && len(items) > 0 {
		return fmt.Errorf("rendered 0 of %d worklist item(s); check the per-VI errors above", len(items))
	}

	slog.Info("toimages runner done", "ok", ok, "failed", failed, "total", len(items))
	return nil
}

// convertOne renders a single VI to frames JSON by shelling out to
// `lvctl toimages <viPath>`. lvctl connects to (or launches) LabVIEW over the
// native VI Server TCP protocol, captures the VI's images, PNG-encodes them in
// Go, and writes the frames JSON array to stdout. The returned string is the
// raw JSON (validated to be a non-empty array) ready for the by-blob store.
func convertOne(lvctlPath, viPath string, timeout time.Duration) (string, error) {
	if _, err := os.Stat(viPath); err != nil {
		return "", fmt.Errorf("VI not found: %w", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()

	var stdout, stderr bytes.Buffer
	cmd := exec.CommandContext(ctx, lvctlPath, "toimages", viPath)
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		// lvctl writes the real LabVIEW / VI-Server error to stderr. Surface a
		// panic-aware summary: when lvctl crashed, start the excerpt at the
		// "panic:" header (the actual cause) instead of the truncated tail, so a
		// per-VI failure is diagnosable in the batch log.
		return "", fmt.Errorf("lvctl toimages failed: %w; stderr: %s", err, diagStderr(stderr.String()))
	}

	out := bytes.TrimSpace(stdout.Bytes())
	if len(out) == 0 {
		return "", fmt.Errorf("lvctl returned empty output")
	}
	// Validate it is a non-empty JSON array of frames before committing it.
	var frames []json.RawMessage
	if err := json.Unmarshal(out, &frames); err != nil {
		return "", fmt.Errorf("lvctl output is not a JSON array: %w", err)
	}
	if len(frames) == 0 {
		return "", fmt.Errorf("lvctl returned zero frames")
	}
	return string(out), nil
}

// lastLines returns the last n non-empty lines of s joined by "; ".
func lastLines(s string, n int) string {
	var lines []string
	for _, ln := range strings.Split(s, "\n") {
		if ln = strings.TrimSpace(ln); ln != "" {
			lines = append(lines, ln)
		}
	}
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	return strings.Join(lines, "; ")
}

// diagStderr trims lvctl's stderr for the single-line batch log. If lvctl
// crashed, a Go runtime traceback ends with the bottom frames (main.main),
// so lastLines would hide the "panic:" header that names the real cause.
// When a panic header is present, the excerpt starts there; the result is
// capped so one bad VI cannot flood the batch log.
func diagStderr(s string) string {
	s = strings.TrimSpace(s)
	if i := strings.Index(s, "panic:"); i >= 0 {
		s = s[i:]
	}
	var lines []string
	for _, ln := range strings.Split(s, "\n") {
		if ln = strings.TrimSpace(ln); ln != "" {
			lines = append(lines, ln)
		}
	}
	out := strings.Join(lines, " | ")
	const max = 4000
	if len(out) > max {
		out = out[:max] + " ...(truncated)"
	}
	return out
}

type item struct{ blob, rel string }

func readWorklist(path string) ([]item, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()

	var out []item
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1024*1024), 1024*1024)
	for sc.Scan() {
		line := sc.Text()
		if strings.TrimSpace(line) == "" {
			continue
		}
		parts := strings.SplitN(line, "\t", 2)
		if len(parts) != 2 {
			continue
		}
		blob := strings.TrimSpace(parts[0])
		rel := strings.TrimSpace(parts[1])
		if blob == "" || rel == "" {
			continue
		}
		out = append(out, item{blob: blob, rel: rel})
	}
	return out, sc.Err()
}

func writeJSON(outByBlob, blob, jsonStr string) error {
	if len(blob) < 2 {
		return fmt.Errorf("blob too short: %q", blob)
	}
	dir := filepath.Join(outByBlob, blob[:2])
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(dir, blob+".json"), []byte(jsonStr), 0o644)
}
