"""Render real Celesto command receipts as an asciinema-style guest recording.

This never executes guest source on the host. Input is preserved, timestamped
SDK evidence; idle gaps are compressed and the capture method is disclosed.
"""

import argparse
import ast
import base64
import hashlib
import json
import math
import shlex
import subprocess
import textwrap
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.config import ROOT
from app.evidence import write_json

WIDTH, HEIGHT, FPS = 1280, 720, 5
BG, PANEL, INK, DIM, ACCENT = "#10151c", "#171f2a", "#e5edf6", "#8d9dae", "#64e6b2"


def load_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def display_command(command):
    try:
        expression = ast.parse(shlex.split(command)[2]).body[1].value.args[0]
        source = base64.b64decode(ast.literal_eval(expression.args[0])).decode()
        first_line = next((line.strip() for line in source.splitlines() if line.strip()), "")
        if "runpy.run_path('/workspace/agent.py'" in source:
            return "guest $ python3 /workspace/agent.py  [resource-limited SDK execution]"
        if "DESIGNED_STEP_7_FAILURE" in source:
            return "guest $ python3 -c \"raise RuntimeError('DESIGNED_STEP_7_FAILURE')\""
        if "guest.unlink()" in source:
            return "guest $ python3  [delete disposable guest canary; probe invisible host path]"
        return f"guest $ python3 -c <audited payload: {len(source)} characters>\n# {first_line[:110]}"
    except (ValueError, IndexError, AttributeError, SyntaxError):
        return "guest $ " + command[:180]


def session_events(row):
    name = row["sandbox_name"]
    start = datetime.fromisoformat(row["details"]["started_at"])
    raw = load_lines(ROOT / "reports" / "evidence" / f"{name}.jsonl")
    audit = [e for e in load_lines(ROOT / "logs" / "commands.jsonl") if e["sandbox_name"] == name]
    events = []
    for event in audit:
        text = (
            "HOOK BLOCKED (not executed): " + event["command"]
            if event["decision"] == "block"
            else display_command(event["command"])
        )
        events.append((event["ts"], text))
    for event in raw:
        kind = event["event"]
        if kind == "command_result":
            output = event.get("stdout", "") + event.get("stderr", "")
            text = output.rstrip() + f"\n[guest exit {event['exit_code']}]"
        elif kind == "policy_applied":
            text = f"CONTROL: step {event['step']} failed -> policy_applied={event['policy']}"
        elif kind == "resumed":
            text = "CONTROL: same computer resumed; checkpoint verified"
        elif kind == "cleanup_confirmed":
            text = "CONTROL: Celesto deletion confirmed; no retained computer"
        elif kind == "containment_probe":
            text = (
                f"CONTROL: destructive request blocked={event['blocked']}; "
                f"host canary unchanged={event['host_untouched']}"
            )
        elif kind == "run_failed":
            text = "CONTROL: reconciliation failed: " + event.get("error_type", "unknown")
        elif kind == "step_completed":
            text = f"CONTROL: step {event['step']}/10 complete"
        elif kind == "provider_attached":
            text = "CONTROL: real Celesto computer " + event["provider_computer_id"]
        else:
            continue
        events.append((event["ts"], text))
    return [
        (max(0, (datetime.fromisoformat(ts) - start).total_seconds()), text)
        for ts, text in sorted(events, key=lambda entry: entry[0])
    ]


def fonts():
    regular = Path("C:/Windows/Fonts/consola.ttf")
    if not regular.exists():
        regular = Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")
    return {size: ImageFont.truetype(str(regular), size) for size in [17, 19, 23, 33]}


def frame(font, title, subtitle, lines, footer):
    picture = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(picture)
    draw.text((38, 24), "CELESTO RECONCILE", fill=ACCENT, font=font[19])
    draw.text((38, 61), title, fill=INK, font=font[33])
    draw.text((38, 107), subtitle[:118], fill=DIM, font=font[17])
    draw.rounded_rectangle((28, 147, WIDTH - 28, HEIGHT - 54), radius=12, fill=PANEL)
    wrapped = []
    for line in lines:
        wrapped.extend(textwrap.wrap(line.expandtabs(4), width=116, replace_whitespace=False) or [""])
    for i, line in enumerate(wrapped[-23:]):
        color = ACCENT if line.startswith(("CONTROL:", "HOOK ", "guest $")) else INK
        draw.text((47, 165 + i * 20), line, fill=color, font=font[17])
    draw.text((38, HEIGHT - 36), footer[:124], fill=DIM, font=font[17])
    return picture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume-run", required=True)
    parser.add_argument("--containment-run", required=True)
    args = parser.parse_args()
    rows = []
    sources = [ROOT / "logs" / "commands.jsonl", ROOT / "reports" / "reliability.json"]
    for run_id in [args.resume_run, args.containment_run]:
        path = ROOT / "reports" / "evidence" / "runs" / f"{run_id}.json"
        row = json.loads(path.read_text(encoding="utf-8"))
        if not row["details"]["cleanup_confirmed"] or not row["details"]["allocation_confirmed"]:
            raise ValueError("A recorded demo chapter must use a cleaned-up, real Celesto run")
        rows.append(row)
        sources += [path, ROOT / "reports" / "evidence" / f"{row['sandbox_name']}.jsonl"]
    if not any(e.get("event") == "policy_applied" for e in rows[0]["details"]["events"]):
        raise ValueError("The recovery chapter must contain an applied recovery policy")
    if not rows[0]["details"]["success"]:
        raise ValueError("The recovery chapter must show a verified successful recovery")
    if not rows[1]["details"].get("containment", {}).get("host_untouched"):
        raise ValueError("The containment chapter must include the observed host canary check")
    reliability = json.loads((ROOT / "reports" / "reliability.json").read_text())
    font = fonts()
    chapters = [session_events(row) for row in rows]
    durations = [
        [
            min(2.0, max(0.4, timestamp - (events[i - 1][0] if i else 0)))
            for i, (timestamp, _) in enumerate(events)
        ]
        for events in chapters
    ]
    total = sum(sum(chapter) for chapter in durations)
    scale = min(1.0, 115 / max(total, 1))
    destination = ROOT / "demo" / "walkthrough.mp4"
    command = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{WIDTH}x{HEIGHT}",
        "-r",
        str(FPS),
        "-i",
        "pipe:0",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(destination),
    ]
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    count = 0

    def emit(picture, seconds):
        nonlocal count
        for _ in range(max(1, math.ceil(seconds * FPS))):
            process.stdin.write(picture.tobytes())
            count += 1

    capture = [
        {
            "version": 2,
            "width": 116,
            "height": 23,
            "title": "Real Celesto SDK guest command receipts; idle gaps compressed",
            "env": {"TERM": "xterm-256color", "SHELL": "Celesto SDK"},
        }
    ]
    try:
        emit(
            frame(
                font,
                "Untrusted code. A separate computer.",
                "Real guest execution, independently checked balances",
                [
                    "Problem: reconcile sales and payments without running generated Python next to business credentials.",
                    "The model writes a generic program. Celesto executes it. The trusted oracle checks exact cents.",
                    "This recording displays actual timestamped SDK commands, guest stdout/stderr and lifecycle receipts.",
                    "Idle gaps are compressed. The source transcripts and hashes are shipped beside this video.",
                    "No model code runs on the host. The model is a free OpenRouter variant with zero-price caps.",
                ],
                "Capture method: evidence-backed guest terminal rendering, not a local shell simulation.",
            ),
            8,
        )
        offset = 0.0
        for chapter_index, (row, events, lengths) in enumerate(zip(rows, chapters, durations, strict=True)):
            title = "1 / Recovery after step 7" if chapter_index == 0 else "2 / Containment in the guest"
            subtitle = row["sandbox_name"] + " | " + row["details"]["provider_computer_id"]
            terminal = []
            for (timestamp, text), length in zip(events, lengths, strict=True):
                capture.append([round(offset + timestamp, 3), "o", text.replace("\n", "\r\n") + "\r\n"])
                terminal.extend(text.splitlines())
                picture = frame(
                    font,
                    title,
                    subtitle,
                    terminal,
                    f"Actual guest elapsed: {timestamp:.1f}s | SDK receipt stream | idle gaps compressed",
                )
                emit(picture, length * scale)
                if "policy_applied=" in text or "HOOK BLOCKED" in text:
                    picture.save(
                        ROOT / "demo" / ("recovery.png" if chapter_index == 0 else "containment.png")
                    )
            offset += row["details"]["wall_clock_s"] + 1
            emit(picture, 3)
        emit(
            frame(
                font,
                "Measured results, preserved failures",
                "Fresh computers; exact oracle checks; confirmed deletion",
                [
                    f"Fresh evaluation computers: {reliability['run_count']}",
                    f"Success rate: {100 * reliability['success_rate']:.1f}%",
                    f"Median wall time: {reliability['median_wall_clock_s']:.2f}s",
                    f"p90 wall time: {reliability['p90_wall_clock_s']:.2f}s",
                    f"Median boot to ready: {reliability['median_boot_to_ready_s']:.2f}s",
                    f"Estimated cohort cost: ${reliability['total_cost_usd']:.6f} (not an invoice)",
                    "Recovery: resume the same disk, verify the checkpoint, repeat only a safe export.",
                    "Containment: raw destructive command blocked; guest-only deletion; host canary hash unchanged.",
                    "Cloud network denial is not claimed. See postmortem.md for SDK limits and failure analysis.",
                ],
                "Evidence: reports/reliability.json, failure_trace.jsonl, containment.json and logs/commands.jsonl",
            ),
            12,
        )
    finally:
        process.stdin.close()
        code = process.wait()
    if code:
        raise RuntimeError("ffmpeg failed")
    (ROOT / "demo" / "guest-session.cast").write_text(
        "\n".join(json.dumps(event) for event in capture) + "\n", encoding="utf-8"
    )
    write_json(
        ROOT / "demo" / "evidence_index.json",
        {
            "capture_method": "Actual Celesto SDK command and guest output receipts rendered as a terminal recording",
            "idle_gaps_compressed": True,
            "duration_s": count / FPS,
            "video_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            "run_ids": [row["run_id"] for row in rows],
            "sources": [
                {
                    "path": str(path.relative_to(ROOT)).replace("\\", "/"),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
                for path in sources
            ],
        },
    )
    print(json.dumps({"video": str(destination), "duration_s": count / FPS}))


if __name__ == "__main__":
    main()
