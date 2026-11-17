import json
import subprocess

from test_ceo_ops import org  # noqa: F401  (fixture)

from agentsorg.intake import find_files, scan_folder


def project(tmp_path):
    p = tmp_path / "shop"
    (p / "api" / "src").mkdir(parents=True)
    (p / "web").mkdir()
    (p / "CLAUDE.md").write_text("Always use pnpm. Never touch the payments module without tests.")
    (p / "api" / "CLAUDE.md").write_text("API rule: every route needs auth.")
    (p / "README.md").write_text("# Shop\nA booking app for clinics.")
    (p / "package.json").write_text(json.dumps({"name": "shop", "scripts": {"test": "vitest", "dev": "vite"}}))
    (p / ".env").write_text("STRIPE_SECRET=sk_live_realsecret999")
    (p / ".env.example").write_text("STRIPE_SECRET=\nDATABASE_URL=postgres://...\n")
    (p / "id_rsa").write_text("PRIVATE")
    (p / "node_modules" / "x").mkdir(parents=True)
    (p / "node_modules" / "x" / "CLAUDE.md").write_text("vendored, ignore")
    (p / "api" / "src" / "app.py").write_text("print(1)")
    return p


def test_scan_reads_rules_and_never_secrets(tmp_path):
    p = project(tmp_path)
    found = find_files(p)
    names = {f.relative_to(p).as_posix() for k in found.values() for f in k}
    assert {"CLAUDE.md", "api/CLAUDE.md", "README.md", "package.json", ".env.example"} <= names
    assert ".env" not in names and "id_rsa" not in names and not any("node_modules" in n for n in names)
    text, seen = scan_folder(p)
    assert "Always use pnpm" in text and "every route needs auth" in text and "vitest" in text
    assert "sk_live_realsecret999" not in text and "STRIPE_SECRET, DATABASE_URL" in text and "PRIVATE" not in text


def test_pack_build_slices_and_reaches_agents(org, tmp_path):
    p = project(tmp_path)
    org.files.add("shop", "engineering", f"this-pc {p}")
    pack = ("# Project\nShop: clinic booking.\n## Conventions and rules\nUse pnpm.\n"
            "## DEPT engineering\n- payments needs tests\n- run `pnpm test`\n## DEPT marketing\n- clinics are the buyers\n")
    org.ceo_reply = pack

    import agentsorg.agents as am
    calls = []
    real = am.run_claude

    def fake(exe, prompt, *, model, system, **k):
        calls.append((model, system[:40]))
        from agentsorg.runtime import RunResult
        return RunResult(text=pack if "SCAN:" in prompt else "ok\nDONE: ok", ok=True, model=model,
                         usage=[{"model": model, "input": 5, "output": 5, "cache_read": 0, "cache_create": 0,
                                 "cost_usd": 0}])
    am.run_claude = fake
    try:
        r = org.intake.build(force=True)
        assert r["llm"] and calls[0][0] == org.c.models[2]
        assert "payments needs tests" in org.intake.pack_path("engineering").read_text()
        assert "clinics are the buyers" in org.intake.pack_path("marketing").read_text()
        assert "DEPT" not in org.intake.pack_path().read_text()
        assert "payments needs tests" in org.system_prompt("engineering", org.c.depts["engineering"].members[0].id)
        assert "clinics are the buyers" in org.system_prompt("marketing", org.c.depts["marketing"].members[0].id)
        n = len(calls)
        assert org.intake.build()["skipped"] and len(calls) == n          # nothing changed: no call
        (p / "README.md").write_text("# Shop v2")
        import os, time
        os.utime(p / "README.md", (time.time() + 5, time.time() + 5))
        assert org.intake.build().get("llm") and len(calls) == n + 1
    finally:
        am.run_claude = real


def test_pack_without_model_is_plain_scan(org, tmp_path):
    p = project(tmp_path)
    org.files.add("shop", "engineering", f"this-pc {p}")
    org.exe = None
    r = org.intake.build(force=True)
    assert not r["llm"] and "Always use pnpm" in org.intake.pack_path().read_text()
