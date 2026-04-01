# Harness Evolution -- Developer Guide

The Meta-Harness is Symbiote's self-evolving system. It collects performance data from every session and uses it to automatically improve agent behavior over time -- without manual prompt engineering.

## Overview

The harness evolution pipeline has three phases:

1. **Foundations** (Phase 1): Score every session automatically, accept host feedback, persist execution traces, generate failure memories
2. **Parameter Tuning** (Phase 2): Auto-calibrate numeric parameters (iteration limits, compaction thresholds, memory shares) based on statistical analysis
3. **Prompt Evolution** (Phase 3): Use an LLM to propose improved instruction texts, validate with guard rails, and auto-rollback if quality drops

---

## SessionScore

Every session is automatically scored when `close_session()` is called. The score is a 0.0-1.0 signal computed from the `LoopTrace` without any LLM call.

### Auto-Score Composition

Three signal layers compose the score:

1. **stop_reason** (did it finish or crash?)
   - `end_turn` = 1.0 (completed normally)
   - `stagnation` = 0.2 (repeated same action)
   - `circuit_breaker` = 0.1 (tool failures)
   - `max_iterations` = 0.0 (exhausted limit)

2. **Iteration efficiency** (only for successful completions)
   - 1-2 iterations: 1.0x multiplier
   - 3-4 iterations: 0.7x multiplier
   - 5+ iterations: 0.4x multiplier

3. **Tool failure rate**
   - Each failure reduces the score: `score *= 1 - failure_rate * 0.3`

Sessions with no tool loop (direct LLM response) score 0.8 by default.

### Final Score with User Feedback

When the host reports feedback via `report_feedback()`, the final score combines both signals:

```
final_score = auto_score * 0.6 + user_score * 0.4
```

### Code Example

```python
# Auto-scoring happens automatically on close_session()
kernel.close_session(session.id)

# Host reports user feedback (optional)
kernel.report_feedback(session.id, score=0.9, source="thumbs_up")
```

---

## FeedbackPort

The `FeedbackPort` protocol lets the host report session quality. The kernel defines the contract; the host decides when and how to call it.

```python
# The kernel implements report_feedback directly
kernel.report_feedback(session_id, score=0.85, source="task_completion")
```

Common feedback sources:
- User thumbs up/down (1.0 / 0.0)
- Task completion rate (0.0-1.0)
- User engagement metrics
- Business KPIs

---

## ParameterTuner

The `ParameterTuner` reads session data and adjusts numeric harness parameters. It uses a tiered activation model to be safe with little data.

### Tiers

| Tier | Min Sessions | What it adjusts |
|------|-------------|-----------------|
| 0 | 0 | Nothing -- defaults unchanged |
| 1 | 5 | Safe-only: increase `max_tool_iterations` if 80%+ hit the limit |
| 2 | 20 | Statistical: compaction threshold, iteration cap with 2x headroom |
| 3 | 50 | Fine tuning: `memory_share` based on tool-heavy vs no-tool session scores |

### Safety Caps

All adjustments are bounded by absolute limits:
- `max_tool_iterations`: 3 to 30
- `compaction_threshold`: 2 to 10
- `memory_share`: minimum 0.20

### Code Example

```python
from symbiote.harness.tuner import ParameterTuner

tuner = ParameterTuner(kernel._storage)

# Analyze without applying
result = tuner.analyze(symbiote_id=sym.id, days=7)
print(f"Tier: {result.tier}, Adjustments: {result.adjustments}")
print(f"Reasons: {result.reasons}")

# Apply if you agree
if result.adjustments:
    tuner.apply(result, kernel.environment)
    print(f"Applied: {result.applied}")
```

### Tuning Rules

**Tier 1 -- max_iterations_too_low**: If > 80% of sessions hit `max_iterations`, increase by 5 (up to cap of 30).

**Tier 1 -- max_iterations_too_high**: If successful sessions complete in few iterations, lower the cap to 2x the observed max.

**Tier 2 -- compaction_threshold**: If avg iterations in successful sessions is below the compaction threshold, lower it so compaction triggers more often.

**Tier 3 -- memory_share**: If tool-heavy sessions score significantly lower than no-tool sessions, reduce `memory_share` by 0.05 to leave more room for tool context.

---

## HarnessEvolver

The `HarnessEvolver` uses an LLM to propose improved versions of instruction texts. It is a batch job -- not called per-request.

### Evolvable Components

Three text components can evolve:

| Component | What it controls | Location |
|-----------|------------------|----------|
| `tool_instructions` | Rules for how the LLM uses tools | ChatRunner system prompt |
| `injection_stagnation` | Message when loop stagnates | LoopController injection |
| `injection_circuit_breaker` | Message when circuit breaker triggers | LoopController injection |

Other components (compaction format, context assembly logic, scoring formula) are structural and do NOT evolve via text -- they require code changes via `StructuralEvolver`.

### How Evolution Works

1. Collect recent sessions: failed (score < 0.5) and successful (score >= 0.8)
2. Summarize session traces (tools used, stop reasons, iterations)
3. Send current text + session summaries to a proposer LLM
4. Validate the proposal with guard rails
5. If valid, persist as a new version

### Guard Rails

- **Max length**: Proposal cannot exceed 2x the current text length
- **Min length**: Must be at least 20 characters
- **CRITICAL preservation**: Lines marked `CRITICAL` in the original must appear in the proposal
- **Format check**: Rejects JSON, code blocks, or Python code

### Minimum Data Requirements

- At least 5 failed sessions (score < 0.5)
- At least 3 successful sessions (score >= 0.8)
- Look-back window: configurable (default 7 days)

### Code Example

```python
from symbiote.harness.evolver import EVOLVABLE_COMPONENTS

# Inject a separate LLM for evolution (recommended)
kernel.set_evolver_llm(cheap_llm)

# Run evolution for each component
for component in EVOLVABLE_COMPONENTS:
    result = kernel.evolve_harness(
        symbiote_id=sym.id,
        component=component,
        default_text="...",  # the hardcoded default text
        days=7,
    )
    print(f"{component}: success={result.success}, reason={result.reason}")
```

---

## HarnessVersionRepository

Versions are stored in the `harness_versions` SQLite table. Each (symbiote_id, component) pair has a linear version history.

### Version Lifecycle

1. **Create**: New version is immediately active; previous versions are deactivated
2. **Score tracking**: Every session updates the active version's running average via `update_score()`
3. **Rollback**: If the new version underperforms after 50+ sessions, it is deactivated and the previous version is reactivated

### Auto-Rollback

Rollback triggers when:
- The active version has 50+ sessions tracked
- Its `avg_score` is more than 0.05 below the parent version's `avg_score`

```python
# Check and auto-rollback
rolled_back = kernel.check_harness_rollback(sym.id, "tool_instructions")
if rolled_back:
    print("Rolled back to previous version")
```

### Inspecting Version History

```python
versions = kernel.harness_versions.list_versions(sym.id, "tool_instructions")
for v in versions:
    print(f"v{v['version']}: active={v['is_active']}, "
          f"avg_score={v['avg_score']:.3f}, sessions={v['session_count']}")
```

---

## BenchmarkRunner

Run automated benchmark tasks to validate symbiote behavior before and after harness changes.

### Grading Strategies

| Strategy | What it checks |
|----------|---------------|
| `tool_called` | Were the expected tools called? Score = matched / expected |
| `param_match` | Were the expected parameters passed? Score = matched / expected |
| `custom` | A custom grading function `(trace) -> float` |

### Code Example

```python
from symbiote.harness.benchmark import BenchmarkRunner, BenchmarkTask

runner = BenchmarkRunner(kernel)

tasks = [
    BenchmarkTask(
        id="weather-tokyo",
        description="What is the weather in Tokyo?",
        expected_tools=["get_weather"],
        expected_params={"city": "Tokyo"},
        grading="param_match",
        timeout=60.0,
    ),
    BenchmarkTask(
        id="search-articles",
        description="Find recent articles about AI safety",
        expected_tools=["search_articles"],
        grading="tool_called",
    ),
    BenchmarkTask(
        id="custom-quality",
        description="Summarize the quarterly report",
        grading="custom",
        custom_grader=lambda trace: 1.0 if trace and trace.stop_reason == "end_turn" else 0.0,
    ),
]

result = runner.run_suite(sym.id, tasks, suite_name="regression")
print(f"Score: {result.avg_score:.2f}, Passed: {result.passed}/{result.total_tasks}")

for r in result.results:
    print(f"  {r.task_id}: {'PASS' if r.passed else 'FAIL'} "
          f"(score={r.score:.2f}, {r.elapsed_ms}ms)")
```

---

## CrossSymbioteLearner

When one symbiote discovers effective harness instructions, other symbiotes with similar tool configurations can benefit.

### How It Works

1. **Tool overlap detection**: Computes Jaccard similarity of tool sets between symbiotes
2. **Candidate selection**: Finds active harness versions with `avg_score >= 0.7` from other symbiotes
3. **Transfer**: Creates a new version on the target symbiote with the source's content

### Transfer Criteria

- Source version must have `avg_score >= 0.7`
- Tool overlap must be >= `min_overlap` (default 0.5)
- Target must NOT already have a custom version for that component

### Code Example

```python
from symbiote.harness.cross_learning import CrossSymbioteLearner

learner = CrossSymbioteLearner(kernel._storage, kernel.harness_versions)

# Find transferable improvements
candidates = learner.find_candidates(
    target_symbiote_id=sym.id,
    min_overlap=0.5,
)

for transfer in candidates:
    print(f"From {transfer.source_symbiote[:8]}: "
          f"{transfer.component} (score={transfer.source_avg_score:.2f}, "
          f"overlap={transfer.tool_overlap:.2f})")

    # Apply the transfer
    new_version = learner.transfer(transfer)
    print(f"  -> Created version {new_version}")
```

---

## StructuralEvolver

For changes that go beyond text (parameter adjustments, strategy swaps, pipeline modifications), the `StructuralEvolver` provides a registry-based system.

```python
from symbiote.harness.structural import StructuralEvolver, StructuralProposal

evolver = StructuralEvolver()

# Register a custom strategy
def my_strategy(storage, symbiote_id):
    # Analyze data, return proposals
    return [
        StructuralProposal(
            id="reduce-compaction",
            component=symbiote_id,
            change_type="parameter",
            description="compaction_threshold",
            current_value=4,
            proposed_value=3,
            confidence=0.8,
            evidence="Avg iterations < compaction threshold",
        )
    ]

evolver.register_strategy(my_strategy)

# Run all strategies
proposals = evolver.propose(kernel._storage, sym.id)

# Apply parameter proposals (other types need manual review)
for p in proposals:
    if p.change_type == "parameter":
        evolver.apply(p, kernel.environment)
```

---

## Putting It All Together

A typical harness evolution workflow for a production deployment:

```python
# 1. Run the parameter tuner (safe, data-driven)
from symbiote.harness.tuner import ParameterTuner

tuner = ParameterTuner(kernel._storage)
result = tuner.analyze(sym.id, days=7)
if result.adjustments:
    tuner.apply(result, kernel.environment)

# 2. Run the prompt evolver (requires LLM)
from symbiote.harness.evolver import EVOLVABLE_COMPONENTS

kernel.set_evolver_llm(cheap_proposer_llm)
for component in EVOLVABLE_COMPONENTS:
    kernel.evolve_harness(sym.id, component, default_text="...")

# 3. Check for rollbacks (after 50+ sessions on new version)
for component in EVOLVABLE_COMPONENTS:
    kernel.check_harness_rollback(sym.id, component)

# 4. Run benchmarks to validate
from symbiote.harness.benchmark import BenchmarkRunner

runner = BenchmarkRunner(kernel)
suite = runner.run_suite(sym.id, my_benchmark_tasks)
assert suite.avg_score >= 0.7, f"Regression detected: {suite.avg_score}"

# 5. Transfer improvements to similar symbiotes
from symbiote.harness.cross_learning import CrossSymbioteLearner

learner = CrossSymbioteLearner(kernel._storage, kernel.harness_versions)
for target_id in other_symbiote_ids:
    candidates = learner.find_candidates(target_id, min_overlap=0.5)
    for transfer in candidates:
        learner.transfer(transfer)
```

This can be wrapped in a cron job, CLI command, or triggered after a batch of sessions completes.
