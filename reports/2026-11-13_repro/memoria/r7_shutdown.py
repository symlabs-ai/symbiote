import subprocess, sys, sqlite3, os
from pathlib import Path
S = Path(__file__).parent
for args in ([], ["--wait"]):
    p = subprocess.run([sys.executable, str(S / "r7_child.py"), *args], capture_output=True, text=True, cwd=S)
    print(f"--- processo {'que espera 1.5s após shutdown' if args else 'que sai logo após shutdown() (padrão CLI)'} ---")
    print(p.stdout.strip()); print("stderr:", [l for l in p.stderr.splitlines() if l.strip()][-3:])
    c = sqlite3.connect(S / "run" / "r7" / "t.db")
    print("skill_review_audit:", c.execute("select count(*) from skill_review_audit").fetchone()[0],
          "| dream_reports:", c.execute("select count(*) from dream_reports").fetchone()[0],
          "| skill criada em disco:", (S / "run" / "r7" / "skills" / "cache-fix").exists())
