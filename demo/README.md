# Guest command recording

`walkthrough.mp4` is rendered from actual timestamped Celesto SDK command dispatches, guest stdout/stderr and lifecycle receipts. It is an asciinema-style terminal rendering of real remote execution, rather than a recording of the Celesto console UI. Idle gaps are compressed and labeled. No model source executes on the host during rendering.

The recording has recovery and containment chapters followed by measured results. `guest-session.cast` preserves receipt timing; `evidence_index.json` names source runs and hashes the original evidence. The video must remain at most 180 seconds. If a passed safety probe is followed by failed reconciliation, the original outcome stays visible.

After collecting real evidence, install Pillow and FFmpeg in the trusted authoring environment:

```powershell
python -m pip install Pillow
python -m scripts.render_demo --resume-run REAL_RUN_ID --containment-run REAL_RUN_ID
ffprobe -v error -show_entries format=duration -of default=nw=1 demo/walkthrough.mp4
```

The renderer refuses runs without a real provider ID and confirmed cleanup. Recovery must have an applied policy and verified success. Containment must have the observed host-canary result. Public artifacts contain synthetic fixtures, with no API keys, terminal tokens or private console URLs.
