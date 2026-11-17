"""Refuse new work for a department once it has used its daily token budget (0 tokens to check)."""
from pathlib import Path

import yaml

CAP = int((yaml.safe_load((Path(__file__).parent / "mod.yaml").read_text()) or {}).get("cap_tokens_per_dept", 50000))


def before_task(task, org):
    used = org.s.one("SELECT COALESCE(SUM(input+output+cache_create),0) t FROM usage "
                     "WHERE dept=? AND day=date('now','localtime')", (task["dept"],))["t"]
    if used >= CAP:
        return {"skip": f"{task['dept']} used {used:,} of its {CAP:,} daily tokens"}
    return None
