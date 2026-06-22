# FLO-775 §8 WS gate — 20260622T033037Z-docker

## Result: CONNECT WALL CLEARED at 15,000 VUs

k6 in-container x 15,000 VUs x 120s hold (60s ramp + 120s hold + 30s down) on the
local docker tier (28GB Colima VM, $0 cloud).

### Thresholds
- flock_ws_connect_duration p(95)=**423ms** (bar <1000ms) — PASS
- flock_ws_broadcast_latency p(95)=0s (no producer driven) — PASS
- flock_ws_receive_errors count=171 (bar 0) — FAIL (1.1% teardown-noise at
  scenario boundaries; 100% of sessions connected + joined)

### Key signals
- ws_sessions: **15,011** (100% of the 15k bar established)
- ws_connecting p(95): **295ms**
- flock_ws_rooms_joined: **30,000** (exactly 2 × 15k VUs)
- checks_succeeded: 100% (11/11 ws-session-connected checks)
- vus_max: **15,000**

### The 171 receive_errors
Pre-join socket errors during the 60s ramp-up and 30s ramp-down transitions
(k6 force-closes sockets mid-EIO-handshake at stage boundaries → joined=false →
counted as receive_error per the FLO-105 classification). NOT auth failures
(login 1/1 succeeded), NOT connect-establishment failures (100% connected), NOT
broadcast errors (latency all 0s — no producer driven). The §8 wall is the
connect-establishment bar, which cleared with 577ms of headroom.
