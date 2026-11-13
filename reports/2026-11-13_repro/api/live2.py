import httpx, sys, pathlib, time, statistics, concurrent.futures as cf, collections
B = "http://127.0.0.1:8008"
admin = pathlib.Path(sys.argv[1]).read_text().strip()
H = {"Authorization": f"Bearer {admin}"}
def one(m, p, **k):
    with httpx.Client(base_url=B, timeout=60) as c: return c.request(m, p, **k)
def timed(m, p, n=40, **k):
    ts = []
    with httpx.Client(base_url=B, timeout=60) as c:
        for _ in range(n):
            t = time.perf_counter(); c.request(m, p, **k); ts.append((time.perf_counter() - t) * 1000)
    ts.sort(); return f"p50={statistics.median(ts):6.1f}ms p95={ts[int(len(ts)*.95)-1]:6.1f}ms max={ts[-1]:6.1f}ms"

sid = one("POST", "/symbiotes", headers=H, json={"name": "probe-perf", "role": "assistant"}).json()["id"]
sess = one("POST", "/sessions", json={"symbiote_id": sid}).json()["id"]
print("== latência sequencial (40 chamadas, keep-alive)")
print("GET /health               ", timed("GET", "/health"))
print("GET /sessions/{id}        ", timed("GET", f"/sessions/{sess}"))
print("GET /symbiotes/{id} (auth)", timed("GET", f"/symbiotes/{sid}", headers=H))
print("GET /api/dashboard        ", timed("GET", "/api/dashboard"))
print("POST /sessions/{id}/msgs  ", timed("POST", f"/sessions/{sess}/messages", json={"role": "user", "content": "x" * 200}))

print("\n== /health sob concorrência: bloqueia o event loop? (32 simultâneas)")
t = time.perf_counter()
with cf.ThreadPoolExecutor(32) as ex: rs = list(ex.map(lambda _: one("GET", "/health").status_code, range(64)))
print(f"64x /health em {time.perf_counter()-t:.2f}s  status={collections.Counter(rs)}")

print("\n== corrida em get-or-create por external_key (40 POST /sessions simultâneos, mesma chave)")
key = f"probe-race-{int(time.time())}"
with cf.ThreadPoolExecutor(40) as ex:
    rs = list(ex.map(lambda _: one("POST", "/sessions", json={"symbiote_id": sid, "external_key": key}), range(40)))
codes = collections.Counter(r.status_code for r in rs)
ids = {r.json()["id"] for r in rs if r.status_code == 201}
print(f"status={dict(codes)}  sessões distintas criadas para a MESMA chave: {len(ids)}")

print("\n== escrita concorrente (200 mensagens, 20 threads)")
t = time.perf_counter()
with cf.ThreadPoolExecutor(20) as ex:
    rs = list(ex.map(lambda i: one("POST", f"/sessions/{sess}/messages", json={"role": "user", "content": f"m{i}"}).status_code, range(200)))
dt = time.perf_counter() - t
print(f"status={dict(collections.Counter(rs))}  {200/dt:.0f} req/s")
n = len(one("GET", f"/api/sessions/{sess}/messages", params={"limit": 500}).json())
print(f"mensagens persistidas: {n} (esperado 240)")

print("\n== corpo grande sem limite")
for mb in (1, 10, 50):
    t = time.perf_counter()
    try:
        r = one("POST", f"/sessions/{sess}/messages", json={"role": "user", "content": "x" * (mb * 1024 * 1024)})
        print(f"mensagem de {mb} MB -> {r.status_code} em {time.perf_counter()-t:.2f}s")
    except Exception as e: print(f"mensagem de {mb} MB -> {type(e).__name__}")
t = time.perf_counter(); r = one("GET", f"/api/sessions/{sess}/messages", params={"limit": 500})
print(f"GET mensagens da sessão depois disso -> {r.status_code}, {len(r.content)/1e6:.0f} MB em {time.perf_counter()-t:.2f}s")
