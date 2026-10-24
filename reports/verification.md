# Controller and live evidence verification

Verified locally on 2026-10-24 using Python 3.13 and the Linux Docker API image.

The initial published commit also passed [GitHub CI on a fresh Ubuntu runner](https://github.com/Rishi1435/Celesto-AI-Hackathon/actions/runs/40083303644), including a clean image build and the live Postgres API integration check. The final read-only [provider inventory](evidence/final_inventory.json) confirms zero retained computers; the earlier provider-limits snapshot describes the account during evaluation.

- **60 tests passed**, including the Postgres-backed live API integration test and all required real-evidence gates. No evidence tests were skipped.
- Ruff code and formatting checks passed.
- Docker Compose builds with `celesto==0.1.8`, initializes Postgres automatically, and reports both services healthy. `/health` returns HTTP 200 with `{"status":"ok"}`.
- Run inserts, validation errors, duplicate IDs, the seed row, authentication and idempotent SQL initialization were checked.
- **20 fresh Celesto computers** were evaluated: 7 succeeded, 12 failed and 1 was intentionally interrupted. All 20 were deleted. A final provider inventory returned zero retained computers.
- Real SDK receipts demonstrate stop/resume persistence, the step-7 recovery policy, destructive-command blocking, guest-only deletion, and an unchanged host canary. Cleanup was observed on success, failure and cancellation.
- Five parallel allocation attempts were rejected by the trial creation limit. Actual allocated jobs ran serially; no five-VM simultaneous execution is claimed.
- The **148.8-second** demo renders actual guest command/output and lifecycle receipts, with compressed idle gaps disclosed. Its manifest hashes the video and original evidence sources.
- Free-only OpenRouter policy remained enabled, without paid fallback. Reported model cost was zero. Sandbox cost in the report is an estimate, not an invoice.

The measured success rate is **35%**. Passing controller and evidence checks does not establish production reliability; the failed runs and model/configuration phases remain in the reports.

Complete verification command:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -u -m pytest -q --live-api --api-base-url http://localhost:18080 --require-evidence
```

The plugin setting avoids unrelated globally installed pytest plugins. Windows has Oracle's `TNSLSNR` on host port 8080, so local validation uses loopback port **18080**, forwarding to container port 8080. The project default remains 8080. No untrusted model or guest program was executed on the host.

Git attributes preserve the original evidence bytes across platforms. Source and video SHA-256 values are checked against the staged repository contents before publication. Provider credentials and the application access token remain only in ignored local configuration.
