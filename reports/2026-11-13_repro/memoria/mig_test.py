import importlib.util, sqlite3, sys, os, glob, shutil, subprocess
from pathlib import Path
S = Path(__file__).parent
from symbiote.adapters.storage.sqlite import SQLiteAdapter as Cur

def cols(db):
    c = sqlite3.connect(db)
    out = {}
    for (t,) in c.execute("select name from sqlite_master where type='table'"):
        out[t] = [r[1] for r in c.execute(f"pragma table_info({t})")]
    idx = sorted(r[0] for r in c.execute("select name from sqlite_master where type='index' and name not like 'sqlite_%'"))
    c.close()
    return out, idx

fresh = S / "mig_fresh.db"
for f in glob.glob(str(fresh)+"*"): os.remove(f)
a = Cur(fresh); a.init_schema(); a.close()
fresh_cols, fresh_idx = cols(fresh)

order = sys.argv[1:]
for h in order:
    p = S / "hist" / f"sqlite_{h}.py"
    spec = importlib.util.spec_from_file_location(f"old_{h}", p)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    db = S / f"mig_{h}.db"
    for f in glob.glob(str(db)+"*"): os.remove(f)
    old = m.SQLiteAdapter(db); old.init_schema(); old.close()
    try:
        new = Cur(db); new.init_schema(); new.close()
    except Exception as e:
        print(h, "BOOT FAILS:", type(e).__name__, e); continue
    c, i = cols(db)
    diffs = []
    for t, cs in fresh_cols.items():
        if t not in c: diffs.append(f"missing table {t}"); continue
        miss = set(cs) - set(c[t]); extra = set(c[t]) - set(cs)
        if miss: diffs.append(f"{t}: missing cols {sorted(miss)}")
        if extra: diffs.append(f"{t}: extra cols {sorted(extra)}")
    mi = set(fresh_idx) - set(i)
    if mi: diffs.append(f"missing indexes {sorted(mi)}")
    print(h, "OK" if not diffs else diffs)
