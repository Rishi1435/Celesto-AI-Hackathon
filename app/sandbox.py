"""The only execution adapter. There is deliberately no host-shell fallback."""

import base64
import json
import shlex
import threading
import time
from pathlib import Path

from app.evidence import append_jsonl, utcnow


class CommandBlockedError(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


class GuestCommandError(RuntimeError):
    pass


def python_command(source):
    payload = base64.b64encode(source.encode()).decode("ascii")
    return "python3 -c " + shlex.quote(f"import base64;exec(base64.b64decode('{payload}'))")


def inspection_metadata(output):
    """Only fixed schema labels and bounded row counts may reach the model."""
    raw = json.loads(output)
    result = {}
    for name, allowed in {
        "sales.csv": {"order_id", "amount", "notes"},
        "payments.csv": {"payment_id", "order_id", "amount"},
    }.items():
        item = raw[name]
        count = item["row_count"]
        if type(count) is not int or not 0 <= count <= 100000:
            raise ValueError("Invalid CSV inspection metadata")
        result[name] = {"columns": sorted(allowed.intersection(item["columns"])), "row_count": count}
    return json.dumps(result)


def create_computer(settings, name):
    from celesto import Computer

    if settings.provider == "cloud":
        return Computer(
            provider="cloud",
            api_key=settings.celesto_key,
            template_id=settings.template,
            lifetime="persistent",
        )
    # Local policy is supported only by Linux Firecracker; never silently downgrade.
    import platform

    if platform.system() != "Linux" or settings.backend != "firecracker":
        raise ValueError("Local execution requires Linux/KVM with Firecracker for network isolation")
    if not settings.local_image or settings.local_image.startswith("replace-"):
        raise ValueError("Local mode needs CELESTO_LOCAL_IMAGE: an Alpine rootfs with Python 3 preinstalled")
    from celesto import Celesto

    return Celesto(
        image=settings.local_image,
        os="alpine",
        backend="firecracker",
        comm_channel="vsock",
        internet_settings={"mode": "off"},
    )


class Sandbox:
    def __init__(self, settings, name, root: Path, cancel: threading.Event, factory=create_computer):
        self.settings, self.name, self.root, self.cancel = settings, name, root, cancel
        self.factory, self.computer = factory, None
        self.approved = set()
        self.boot_s = 0.0
        self.created = False
        self.deleted = False
        self.start_attempted = False
        self.start_complete = False
        self.allocation_rejected = False
        self.cleanup_not_required = False

    def check_cancelled(self):
        if self.cancel.is_set():
            raise Cancelled("Run interrupted; cleanup requested")

    def create(self):
        self.check_cancelled()
        start = time.perf_counter()
        self.computer = self.factory(self.settings, self.name)
        self.created = True
        # The pinned SDK discards unknown 429 response details. Preserve a bounded,
        # redacted diagnostic for allocation failures so operators can honor cooldowns.
        provider = getattr(self.computer, "_provider", None)
        transport = getattr(provider, "_client", None)
        if transport is not None and hasattr(transport, "get_httpx_client"):

            def allocation_response(response):
                if (
                    response.status_code < 400
                    or response.request.method != "POST"
                    or response.request.url.path != "/v1/computers"
                ):
                    return
                response.read()
                diagnostic = response.text[:2000]
                for secret in [self.settings.celesto_key, self.settings.llm_key, self.settings.access_token]:
                    if secret:
                        diagnostic = diagnostic.replace(secret, "[redacted]")
                append_jsonl(
                    self.root / "reports" / "evidence" / "provider_errors.jsonl",
                    {
                        "ts": utcnow(),
                        "sandbox_name": self.name,
                        "http_status": response.status_code,
                        "retry_after": response.headers.get("retry-after"),
                        "diagnostic": diagnostic,
                    },
                )

            transport.get_httpx_client().event_hooks["response"].append(allocation_response)
        self.start_attempted = True
        try:
            self.computer.start()
        except Exception as error:
            # A structured 4xx rejection with no side effects differs from a lost create response.
            self.allocation_rejected = (
                getattr(error, "operation", None) == "create"
                and getattr(error, "side_effects", None) == "none"
                and 400 <= getattr(error, "status_code", 0) < 500
            )
            raise
        self.start_complete = True
        self.python("print('ready')")
        self.boot_s = time.perf_counter() - start

    def request(self, command):
        """Fail-closed pre-execution safety hook. Audit before calling the SDK."""
        self.check_cancelled()
        decision = "allow" if command in self.approved else "block"
        append_jsonl(
            self.root / "logs" / "commands.jsonl",
            {
                "ts": utcnow(),
                "sandbox_name": self.name,
                "command": command,
                "decision": decision,
                "execution_target": "celesto_sandbox",
            },
        )
        if decision == "block":
            raise CommandBlockedError("Only commands rendered by structured tools are permitted")
        result = self.computer.run(command, timeout=self.settings.command_timeout)

        def field(key, default):
            return result.get(key, default) if isinstance(result, dict) else getattr(result, key, default)

        code = field("exit_code", None)
        if code is None:
            raise GuestCommandError("SDK returned no exit code; action outcome unknown; do not retry")
        stdout, stderr = str(field("stdout", ""))[:20000], str(field("stderr", ""))[:8000]
        append_jsonl(
            self.root / "reports" / "evidence" / f"{self.name}.jsonl",
            {
                "ts": utcnow(),
                "event": "command_result",
                "exit_code": code,
                "stdout": stdout,
                "stderr": stderr,
            },
        )
        if code != 0:
            raise GuestCommandError(f"Guest exit {code}: {stderr[-3000:]}")
        return stdout

    def python(self, source):
        command = python_command(source)
        self.approved.add(command)
        try:
            return self.request(command)
        finally:
            self.approved.discard(command)

    def write_files(self, files):
        if any(name not in {"sales.csv", "payments.csv", "agent.py", "checkpoint.json"} for name in files):
            raise ValueError("Tool file path is outside the fixed workspace allowlist")
        return self.python(
            "from pathlib import Path\n"
            "p=Path('/workspace');p.mkdir(exist_ok=True)\n"
            f"files={files!r}\n"
            "for name,content in files.items(): (p/name).write_text(content,encoding='utf-8')\n"
            "print('files written')"
        )

    def inspect(self):
        output = self.python(
            "import csv,json\nfrom pathlib import Path\nresult={}\n"
            "for name,allowed in {'sales.csv':['order_id','amount','notes'],"
            "'payments.csv':['payment_id','order_id','amount']}.items():\n"
            " with (Path('/workspace')/name).open() as f:\n"
            "  reader=csv.DictReader(f)\n"
            "  columns=[column for column in allowed if column in (reader.fieldnames or [])]\n"
            "  result[name]={'columns':columns,'row_count':sum(1 for _ in reader)}\n"
            "print(json.dumps(result))"
        )
        return inspection_metadata(output)

    def execute_agent(self, source):
        if len(source.encode()) > 30000:
            raise ValueError("Agent script exceeds 30 KB")
        # Cloud exec has a 10,000-character command limit; stage large scripts in chunks.
        self.write_files({"agent.py": ""})
        for offset in range(0, len(source), 2000):
            chunk = source[offset : offset + 2000]
            self.python(
                "from pathlib import Path\np=Path('/workspace/agent.py')\n"
                f"with p.open('a',encoding='utf-8') as f:f.write({chunk!r})"
            )
        return self.python(
            "import os,resource,runpy\n"
            "os.chdir('/workspace')\n"
            "resource.setrlimit(resource.RLIMIT_CPU,(10,10))\n"
            "resource.setrlimit(resource.RLIMIT_AS,(536870912,536870912))\n"
            "resource.setrlimit(resource.RLIMIT_FSIZE,(2097152,2097152))\n"
            "resource.setrlimit(resource.RLIMIT_NPROC,(32,32))\n"
            "runpy.run_path('/workspace/agent.py',run_name='__main__')\n"
            "print('agent program completed')"
        )

    def read_report(self):
        output = self.python(
            "from pathlib import Path\n"
            "p=Path('/workspace/reconciled.json')\n"
            "assert p.stat().st_size<=16000,'report too large'\n"
            "print(p.read_text())"
        )
        return json.loads(output)

    def restart_and_verify(self, expected):
        self.check_cancelled()
        self.computer.stop()
        self.check_cancelled()
        if self.settings.provider == "cloud":
            self.computer.resume()
        else:
            self.computer.start()
        actual = self.python("from pathlib import Path;print(Path('/workspace/checkpoint.json').read_text())")
        if json.loads(actual) != expected:
            raise ValueError("Checkpoint changed across stop/start")

    def cleanup(self):
        if not self.created:
            return
        if self.allocation_rejected:
            self.computer.delete()  # Releases the deferred SDK handle; no guest was allocated.
            self.cleanup_not_required = True
            return
        provider_id = getattr(self.computer, "id", None) or getattr(self.computer, "vm_id", None)
        if self.start_attempted and not self.start_complete and provider_id is None:
            raise RuntimeError(
                "Allocation outcome unknown: check the Celesto console before starting another job"
            )
        # Deletion is idempotent by identity. Unknown command outcomes are never retried.
        errors = []
        for attempt in range(3):
            try:
                assert self.computer is not None
                self.computer.delete()
                self.deleted = True
                return
            except Exception as error:
                errors.append(type(error).__name__)
                time.sleep(0.2 * (attempt + 1))
        raise RuntimeError(
            "Cleanup not confirmed; manual deletion needed for " + self.name + ": " + ",".join(errors)
        )
