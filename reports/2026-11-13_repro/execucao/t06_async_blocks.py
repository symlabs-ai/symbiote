"""message_async runs the (sync) LLM call and retry sleeps on the event loop thread."""
import asyncio, time, logging
from common import *
logging.disable(logging.CRITICAL)

class SlowLLM:
    def __init__(self, fail_first=False): self.n = 0; self.fail_first = fail_first
    def complete(self, messages, config=None, tools=None):
        self.n += 1
        if self.fail_first and self.n == 1:
            raise ConnectionError("503 upstream")       # retryable -> time.sleep(1) in _call_llm_with_retry
        time.sleep(0.5)                                  # stands for a blocking HTTP call
        return "ok"

async def main(fail_first):
    k = mk_kernel(SlowLLM(fail_first))
    sym = k.create_symbiote("a", "assistant")
    sessions = [k.start_session(sym.id) for _ in range(4)]
    gaps = []; stop = False
    async def heartbeat():
        last = time.monotonic()
        while not stop:
            await asyncio.sleep(0.01)
            now = time.monotonic(); gaps.append(now - last); last = now
    hb = asyncio.create_task(heartbeat())
    t0 = time.monotonic()
    await asyncio.gather(*(k.message_async(s.id, "oi") for s in sessions))
    total = time.monotonic() - t0
    stop = True; await hb
    print(f"fail_first={fail_first}: 4 sessoes distintas em paralelo, LLM 0.5s cada -> total={total:.2f}s "
          f"(paralelo real seria ~0.5s); maior congelamento do event loop={max(gaps):.2f}s")
asyncio.run(main(False)); asyncio.run(main(True))
