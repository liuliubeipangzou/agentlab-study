"""A bounded async DAG runner; steps can be functions or calls to other agents."""

import asyncio
import copy
import inspect
import math
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional


@dataclass
class WorkflowStep:
    name: str
    run: Callable[[Dict[str, Any]], Awaitable[Any]]
    depends_on: List[str] = field(default_factory=list)
    retries: int = 0
    timeout: float = 30


@dataclass
class WorkflowResult:
    outputs: Dict[str, Any]
    errors: Dict[str, str]
    statuses: Dict[str, str]

    @property
    def ok(self) -> bool:
        return all(status == "success" for status in self.statuses.values())


class Workflow:
    """Execute a validated DAG while allowing independent branches to finish.

    A step receives an isolated snapshot of ``initial`` and its declared dependency
    outputs. Each retry gets a fresh snapshot. Inputs must support deepcopy.
    Retries are opt-in and require idempotent steps.
    Timeout applies to each attempt. Caller cancellation cancels active steps.
    """

    def __init__(self, steps: List[WorkflowStep], concurrency: int = 3) -> None:
        if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency < 1:
            raise ValueError("concurrency must be a positive integer")
        # Copy the step list and dependencies so later caller edits cannot change the DAG.
        self.steps = [WorkflowStep(step.name, step.run, list(step.depends_on), step.retries, step.timeout)
                      for step in steps]
        self.concurrency = concurrency
        names = [step.name for step in self.steps]
        if any(not isinstance(name, str) or not name for name in names):
            raise ValueError("step names must be nonempty strings")
        if len(set(names)) != len(names):
            raise ValueError("duplicate workflow step name")
        by_name = {step.name: step for step in self.steps}
        for step in self.steps:
            if not callable(step.run):
                raise TypeError("step {} must have a callable run".format(step.name))
            if not isinstance(step.retries, int) or isinstance(step.retries, bool) or step.retries < 0:
                raise ValueError("step retries must be a nonnegative integer")
            if type(step.timeout) not in (int, float) or not math.isfinite(step.timeout) or step.timeout <= 0:
                raise ValueError("step timeout must be finite and positive")
            missing = set(step.depends_on) - set(by_name)
            if missing:
                raise ValueError("step {} has missing dependencies: {}".format(step.name, sorted(missing)))
        # Kahn's algorithm also detects self-dependencies without recursion limits.
        remaining = {step.name: set(step.depends_on) for step in self.steps}
        while remaining:
            ready = {name for name, dependencies in remaining.items() if not dependencies}
            if not ready:
                raise ValueError("workflow contains a dependency cycle")
            remaining = {name: dependencies - ready for name, dependencies in remaining.items() if name not in ready}

    async def _execute(self, step: WorkflowStep, outputs: Dict[str, Any]) -> Any:
        async def invoke() -> Any:
            result = step.run(copy.deepcopy(outputs))
            if not inspect.isawaitable(result):
                raise TypeError("workflow step run must return an awaitable")
            return await result

        for attempt in range(step.retries + 1):
            try:
                return await asyncio.wait_for(invoke(), timeout=step.timeout)
            except asyncio.CancelledError:
                raise
            except Exception:
                if attempt == step.retries:
                    raise
                await asyncio.sleep(min(0.05 * 2 ** min(attempt, 6), 1.0))

    async def run(self, initial: Optional[Dict[str, Any]] = None) -> WorkflowResult:
        if initial is not None and not isinstance(initial, dict):
            raise TypeError("initial must be a dictionary")
        inputs = copy.deepcopy(initial or {})
        outputs = copy.deepcopy(inputs)
        if set(outputs) & {step.name for step in self.steps}:
            raise ValueError("initial output keys must not collide with step names")
        statuses = {step.name: "pending" for step in self.steps}
        errors = {}
        active = {}
        try:
            while any(status in ("pending", "running") for status in statuses.values()):
                # Propagate skips even when declarations are not topologically ordered.
                while True:
                    skipped = False
                    for step in self.steps:
                        if statuses[step.name] != "pending":
                            continue
                        blocked = [dep for dep in step.depends_on if statuses[dep] in ("failed", "skipped")]
                        if blocked:
                            statuses[step.name] = "skipped"
                            errors[step.name] = "Dependency failed or skipped: " + ", ".join(blocked)
                            skipped = True
                    if not skipped:
                        break
                for step in self.steps:
                    if len(active) >= self.concurrency:
                        break
                    if statuses[step.name] == "pending" and all(
                        statuses[dep] == "success" for dep in step.depends_on
                    ):
                        statuses[step.name] = "running"
                        step_inputs = dict(inputs)
                        step_inputs.update({name: outputs[name] for name in step.depends_on})
                        task = asyncio.create_task(self._execute(step, copy.deepcopy(step_inputs)))
                        active[task] = step
                if not active:
                    break
                done, _ = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    step = active.pop(task)
                    try:
                        outputs[step.name] = task.result()
                        statuses[step.name] = "success"
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        statuses[step.name] = "failed"
                        if isinstance(exc, asyncio.TimeoutError):
                            errors[step.name] = "TimeoutError: exceeded {} seconds per attempt".format(step.timeout)
                        else:
                            errors[step.name] = "{}: {}".format(type(exc).__name__, exc)
        finally:
            for task in active:
                task.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)
        return WorkflowResult(outputs=outputs, errors=errors, statuses=statuses)
