Copy `.env.example` to `config/runtime.env`, then replace the placeholders locally. This file is ignored by Git and Docker's build context. Compose loads it at container startup; restart the API after editing it. The root `.env` must remain absent.

Keep credentials in the trusted API process. No keys, host environment variables, home directories, or Docker sockets are injected into the Celesto computer. `API_ACCESS_TOKEN` protects job creation and cancellation. Ledger endpoints remain accessible on the loopback-bound local port for the challenge verifier.
