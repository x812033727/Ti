# Layer 3 Task 4 Live Monitor Evidence - 2026-07-20

Scope: live `/opt/ti/autopilot/status.json`, deployed
`/usr/local/sbin/ti-layer3-monitor.sh`, three consecutive transient systemd
runs. The production pause flag was not removed; each run overrides only
`TI_LAYER3_PAUSE_FILE` to a workspace path so the deployed script evaluates the
live liveness predicate.

## Environment

```text
pause_file=/opt/ti/AUTOPILOT_PAUSED mtime=2026-07-20 02:20:54.642843843 +0800 owner=root:root mode=644
ti-autopilot.service NRestarts=0
```

Repository and deployed script hashes matched before capture:

```text
a85e3e3dd98693977bec851c973dedb3e1fb1c933c8c38758924cc67c6dc8c89  /usr/local/sbin/ti-layer3-monitor.sh
a85e3e3dd98693977bec851c973dedb3e1fb1c933c8c38758924cc67c6dc8c89  deploy/ti-layer3-monitor.sh
dd466427aafd6b6f4f17fa21ba1cbf04438136db04c91e58eb5642cd735f7332  /usr/local/sbin/ti-layer3-liveness.py
dd466427aafd6b6f4f17fa21ba1cbf04438136db04c91e58eb5642cd735f7332  deploy/ti-layer3-liveness.py
```

## Transient Systemd Journal

Command shape:

```bash
timeout 60 systemd-run --wait --collect \
  --unit="ti-layer3-task4-<timestamp>-<round>" \
  --property=Environment=TI_LAYER3_PAUSE_FILE="$PWD/.ti-layer3-no-pause" \
  --property=Environment=TI_LAYER3_STATE_DIR="$PWD/.qa-layer3-live-state" \
  /bin/bash /usr/local/sbin/ti-layer3-monitor.sh
```

Journal capture:

```text
2026-07-20T03:41:09+08:00 srv1501416 systemd[1]: Started ti-layer3-task4-20260720034109-1.service - /bin/bash /usr/local/sbin/ti-layer3-monitor.sh.
2026-07-20T03:41:09+08:00 srv1501416 bash[721432]: layer3: all green (liveness: verdict=alive state=running updated_age_s=3 last_activity_age_s=3 cpu_active=true)
2026-07-20T03:41:09+08:00 srv1501416 systemd[1]: ti-layer3-task4-20260720034109-1.service: Deactivated successfully.
2026-07-20T03:41:11+08:00 srv1501416 systemd[1]: Started ti-layer3-task4-20260720034111-2.service - /bin/bash /usr/local/sbin/ti-layer3-monitor.sh.
2026-07-20T03:41:11+08:00 srv1501416 bash[721563]: layer3: all green (liveness: verdict=alive state=running updated_age_s=2 last_activity_age_s=2 cpu_active=true)
2026-07-20T03:41:11+08:00 srv1501416 systemd[1]: ti-layer3-task4-20260720034111-2.service: Deactivated successfully.
2026-07-20T03:41:13+08:00 srv1501416 systemd[1]: Started ti-layer3-task4-20260720034113-3.service - /bin/bash /usr/local/sbin/ti-layer3-monitor.sh.
2026-07-20T03:41:13+08:00 srv1501416 bash[721677]: layer3: all green (liveness: verdict=alive state=running updated_age_s=4 last_activity_age_s=4 cpu_active=true)
2026-07-20T03:41:13+08:00 srv1501416 systemd[1]: ti-layer3-task4-20260720034113-3.service: Deactivated successfully.
```

Result: three live monitor rounds, three `verdict=alive`, zero monitor restarts,
`ti-autopilot.service NRestarts=0`.

## Direct Live Liveness Samples

```text
verdict=alive state=running updated_age_s=0 last_activity_age_s=0 cpu_active=true
verdict=alive state=running updated_age_s=1 last_activity_age_s=4 cpu_active=true
verdict=alive state=running updated_age_s=6 last_activity_age_s=10 cpu_active=true
```

## Boundary

The live autopilot status was active and fresh during this capture, so this
evidence does not cover a production window with
`last_activity_age_s >= 300 && cpu_active=true`. That stale-activity edge remains
covered by the in-repo regression test
`tests/autopilot/test_qa_layer3_task4_three_rounds.py` until a matching live
window is available.
