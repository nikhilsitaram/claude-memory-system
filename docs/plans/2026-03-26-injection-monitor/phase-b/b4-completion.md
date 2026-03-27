# B4: Add Monitor Tab to Web Frontend

## Status: Complete

## Changes Made

**File modified:** `templates/web/index.html` (+272 lines)

### 1. Nav bar
- Added "Monitor" button after "Graph" in the `#nav` bar

### 2. Monitor section HTML
- New `<section id="section-monitor">` with:
  - Session selector dropdown (`#monitor-session`) with `onchange` handler
  - Auto-scroll checkbox (`#monitor-autoscroll`, checked by default)
  - Timeline container (`#monitor-timeline`)
  - Empty state message (`#monitor-empty`)

### 3. CSS styles added
- `.monitor-entry` - card-style container for each log entry
- `.entry-header` - flex row with hook badge, session ID, timestamp, latency
- `.hook-badge`, `.hook-badge-session` (blue), `.hook-badge-recall` (green)
- `.monitor-tier` with `.tier-name`, `.tier-bar`, `.tier-fill`, `.tier-count` - horizontal bar chart for tier breakdown
- `.monitor-ids`, `.monitor-id-chip` - clickable memory ID chips
- `.monitor-injected` (green), `.monitor-filtered` (gray) - color-coded chip states
- `.monitor-prompt-preview` - truncated prompt display
- `.monitor-latency` - right-aligned latency display

### 4. JavaScript functions added
- `startMonitorPolling()` - resets state, kicks off initial load, sets 2s `setInterval`
- `stopMonitorPolling()` - clears interval
- `onMonitorSessionChange()` - resets timeline and reloads when session filter changes
- `loadMonitor()` - fetches `/api/injection-log?since=...&session_id=...`, appends entries, updates session dropdown, auto-scrolls
- `renderMonitorEntry(entry)` - renders SessionStart (tier bars + ID chips) and PromptRecall (prompt preview + injected/filtered chips)
- `visibilitychange` listener - pauses polling when browser tab hidden, resumes when visible

### 5. showSection() updated
- Stops monitor polling when leaving monitor tab
- Starts monitor polling when entering monitor tab

## Verification
```
python3 -c "from pathlib import Path; html=Path('templates/web/index.html').read_text(); assert 'section-monitor' in html; assert 'injection-log' in html; assert 'setInterval' in html; assert 'visibilitychange' in html; print('All assertions pass')"
```
All assertions pass.

## Design decisions
- Used `var` and traditional `for` loops in monitor JS to match vanilla JS compatibility pattern (no arrow functions in rendering functions that build HTML strings with concatenation)
- Used `document.createElement` in `renderMonitorEntry` instead of `innerHTML` for the timeline container to avoid re-parsing existing DOM nodes on each poll
- Session dropdown populated incrementally as new session_ids appear in log entries
- `_monitorLastTs` tracks the latest timestamp to request only newer entries on subsequent polls
- All user-provided content escaped via `escHtml()` before rendering
- Click-to-detail uses `openDetail()` (not `openModal()`) as specified
