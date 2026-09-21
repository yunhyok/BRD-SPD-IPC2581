"""Human-readable and structured conversion evidence with bounded warning samples."""
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


class Report:
    def __init__(self, output):
        self.output = Path(output)
        self.counts = Counter()
        self.warnings = {}
        self.data = {"tool": "BRD-SPD-IPC2581", "version": "0.1.0",
                     "started_utc": datetime.now(timezone.utc).isoformat(),
                     "status": "running", "allegro_import_verified": False}

    def warn(self, code, message, line=None):
        item = self.warnings.setdefault(code, {"count": 0, "samples": []})
        item["count"] += 1
        if len(item["samples"]) < 5:
            item["samples"].append({"line": line, "message": str(message)})

    def unsupported(self, code, line, text):
        self.warn(code, text, line)

    def save(self, status, **details):
        self.data.update(details)
        self.data.update(status=status, counts=dict(self.counts), warnings=self.warnings)
        json_path = self.output.with_suffix(self.output.suffix + ".report.json")
        log_path = self.output.with_suffix(self.output.suffix + ".log")
        self.data["report_path"] = str(json_path.resolve())
        self.data["log_path"] = str(log_path.resolve())
        json_path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = [f"BRD-SPD-IPC2581: {status}", f"Output: {self.output.resolve()}",
                 "IPC schema compliance does not imply Allegro native design equivalence.",
                 "", "COUNTS", *[f"{k}: {v}" for k, v in self.counts.items()], "", "WARNINGS / LOSSES"]
        for code, item in self.warnings.items():
            lines.append(f"[{code}] occurrences={item['count']}")
            for sample in item["samples"]:
                lines.append(f"  line={sample['line']}: {sample['message']}")
        if "error" in details:
            lines += ["", "ERROR: " + str(details["error"])]
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return self.data
