import time, logging
from symbiote.runners.chat import ChatRunner
from symbiote.core.exceptions import LLMError
from symbiote.adapters.llm import forge
logging.disable(logging.CRITICAL)
f = ChatRunner._is_retryable_error
cases = [
 ("ConnectionError cru", ConnectionError("Connection refused")),
 ("mesmo erro como o ForgeLLMAdapter entrega (LLMError(str(exc)))", LLMError(str(ConnectionError("Connection refused")))),
 ("LLMError('Connection error.')  [msg do APIConnectionError do SDK openai]", LLMError("Connection error.")),
 ("LLMError('Error code: 500 - Internal Server Error')", LLMError("Error code: 500 - Internal Server Error")),
 ("LLMError('Error code: 504 - Gateway Time-out')", LLMError("Error code: 504 - Gateway Time-out")),
 ("LLMError('Error code: 529 - overloaded_error')", LLMError("Error code: 529 - overloaded_error")),
 ("LLMError 400 nao-transiente contendo '503' por acaso", LLMError("Error code: 400 - prompt has 150370 tokens, limit 128000")),
 ("LLMError 400 'unknown parameter: timeout'", LLMError("Error code: 400 - unknown parameter: timeout")),
 ("503 cujo request-id contem '429'", LLMError("Error code: 503 - upstream unavailable (request_id=req_8429a1)")),
 ("FileNotFoundError (subclasse de OSError)", FileNotFoundError("ca-bundle.crt")),
]
for name, exc in cases:
    print(f"retry={f(exc)!s:5} <- {name}")

class L:
    n = 0
    def complete(self, messages, config=None, tools=None):
        L.n += 1; raise LLMError("Error code: 400 - prompt has 150370 tokens, limit 128000")
r = ChatRunner(L()); t0 = time.monotonic()
try: r._call_llm_with_retry([], {}, None)
except Exception: pass
print(f"erro 400 deterministico: {L.n} chamadas ao LLM, {time.monotonic()-t0:.1f}s bloqueado em time.sleep")
import inspect
print("ForgeLLMAdapter cria ChatAgent com max_retries=0:", '"max_retries": 0' in inspect.getsource(forge.ForgeLLMAdapter.__init__))
