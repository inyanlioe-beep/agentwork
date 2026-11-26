import asyncio
from collections.abc import Awaitable, Callable
import json
import logging

from pydantic import ValidationError

from app.bus import EventBus
from app.config import Settings
from app.db import Database
from app.models import Event, Message, PlanStep, Task, TaskPlan, utc_now
from app.providers.registry import create_provider
from app.agents.runtime import AgentRuntime

logger = logging.getLogger(__name__)


class TaskCancelledByUser(Exception):
    """Raised when a workflow notices that its task was explicitly cancelled.

    The cancel endpoint stamps ``cancelled_by_user`` and stops the tracked
    workflow coroutine. A workflow that keeps running (for example because its
    coroutine was not tracked yet) must not resurrect the task, so status writes
    check the persisted flag first and surface this error instead.
    """

    def __init__(self, task_id: str) -> None:
        super().__init__(f"Task {task_id} was cancelled by the user")
        self.task_id = task_id


class Orchestrator:
    def __init__(
        self,
        database: Database,
        bus: EventBus,
        settings: Settings,
        *,
        spawn_task: Callable[[str, Awaitable[None]], None] | None = None,
    ) -> None:
        self.database = database
        self.bus = bus
        self.settings = settings
        self._human_waiters: dict[str, tuple[asyncio.Future[str], str]] = {}
        self._answer_lock = asyncio.Lock()
        self._spawn_task = spawn_task

    def _setup_waiter_for_task(self, task: Task) -> None:
        if task.status != "waiting_input":
            return
        if task.waiting_step_index is None or task.waiting_agent_id is None:
            return
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[str] = loop.create_future()
        self._human_waiters[task.id] = (waiter, task.waiting_agent_id)
        logger.info("Set up waiter for task %s, step %d, agent %s", task.id, task.waiting_step_index, task.waiting_agent_id)

    def restore_waiting_waiter(self, task: Task) -> None:
        """Re-create the in-memory waiter for a task that was waiting for human input."""
        self._setup_waiter_for_task(task)

    def start_recovery(self, task: Task) -> None:
        """Schedule best-effort recovery for a task that a restart interrupted."""
        self._spawn(task.id, self.resume_interrupted_task(task))

    def _spawn(self, task_id: str, coro: Awaitable[None]) -> None:
        """Run background workflow work through the application task tracker.

        Recovered workflows must be registered exactly like freshly created ones,
        otherwise ``POST /api/tasks/{task_id}/cancel`` cannot stop them and the
        workflow would keep writing status updates over the cancellation.
        """

        async def runner() -> None:
            try:
                await coro
            except TaskCancelledByUser:
                logger.info("Task %s stopped after explicit user cancellation", task_id)

        if self._spawn_task is not None:
            self._spawn_task(task_id, runner())
        else:
            asyncio.create_task(runner())

    async def _is_cancelled_by_user(self, task: Task) -> bool:
        persisted = await self.database.get_task(task.id)
        return persisted is not None and persisted.cancelled_by_user

    @staticmethod
    def _step_message_id(task_id: str, step_index: int, kind: str) -> str:
        """Deterministic message id so a replayed step overwrites instead of duplicating."""
        return f"{task_id}:step:{step_index}:{kind}"

    async def run(self, task: Task) -> Task:
        try:
            await self._set_status(task, "planning")
            orchestrator_config = await self._get_orchestrator()
            planner = AgentRuntime(
                orchestrator_config,
                create_provider(
                    orchestrator_config.provider,
                    model=orchestrator_config.model,
                    settings=self.settings,
                ),
                timeout_seconds=self.settings.timeout_seconds,
                max_retries=self.settings.max_retries,
            )
            await self._message(
                task.id,
                "human",
                orchestrator_config.id,
                "instruction",
                task.instruction,
                message_id=f"{task.id}:instruction",
            )
            plan = await self._make_plan(planner, task)
            steps = [
                PlanStep(
                    task_id=task.id,
                    index=index,
                    description=item.description,
                    agent_id=item.agent_id,
                )
                for index, item in enumerate(plan.steps, start=1)
            ]
            for step in steps:
                await self.database.save_step(step)
                await self._publish(
                    Event(task_id=task.id, type="step_update", payload=step.model_dump(mode="json"))
                )

            await self._set_status(task, "running")
            results: list[str] = []
            for step in steps:
                agent = await self.database.get_agent(step.agent_id)
                if agent is None or not agent.enabled or agent.role != "worker":
                    raise ValueError(f"Plan assigned an unavailable worker: {step.agent_id}")

                # Store current execution state for resume after shutdown
                task.cancelled_step_index = step.index
                task.cancelled_context = f"Original request: {task.instruction}\nEarlier results:\n" + "\n".join(results)
                await self.database.save_task(task)
                
                step.status = "in_progress"
                await self.database.save_step(step)
                await self._publish(
                    Event(task_id=task.id, type="step_update", payload=step.model_dump(mode="json"))
                )
                await self._message(
                    task.id,
                    orchestrator_config.id,
                    agent.id,
                    "assignment",
                    step.description,
                    message_id=self._step_message_id(task.id, step.index, "assignment"),
                )
                worker = AgentRuntime(
                    agent,
                    create_provider(agent.provider, model=agent.model, settings=self.settings),
                    timeout_seconds=self.settings.timeout_seconds,
                    max_retries=self.settings.max_retries,
                )
                context = (
                    f"Original request: {task.instruction}\nEarlier results:\n"
                    + "\n".join(results)
                )
                question_count = 0
                while True:
                    result = await self._stream_message(
                        task.id,
                        agent.id,
                        orchestrator_config.id,
                        "response",
                        worker,
                        step.description,
                        context=context,
                        suppress_prefix="[NEEDS_INPUT]:",
                        message_id=self._step_message_id(task.id, step.index, "response"),
                    )
                    if not result.strip().startswith("[NEEDS_INPUT]:"):
                        break
                    question = result.split(":", 1)[1].strip()
                    if not question:
                        raise ValueError(f"Agent {agent.id} returned an empty input request")
                    if len(question) > 4_000:
                        raise ValueError(f"Agent {agent.id} returned an input request that is too long")
                    question_count += 1
                    if question_count > 3:
                        raise ValueError(f"Agent {agent.id} exceeded the input request limit")
                    answer = await self._ask_human(task, step, agent.id, question)
                    step.status = "in_progress"
                    step.result = None
                    await self.database.save_step(step)
                    await self._publish(
                        Event(task_id=task.id, type="step_update", payload=step.model_dump(mode="json"))
                    )
                    context += f"\nHuman answer: {answer}"
                results.append(f"{agent.name}: {result}")
                step.status = "done"
                step.result = result
                await self.database.save_step(step)
                await self._publish(
                    Event(task_id=task.id, type="step_update", payload=step.model_dump(mode="json"))
                )

            await self._set_status(task, "review")
            summary = await self._stream_message(
                task.id,
                orchestrator_config.id,
                "human",
                "summary",
                planner,
                "Write the final summary for the human using the completed work below. "
                "Be clear and do not claim facts not present in the work.",
                context="\n\n".join(results),
                message_id=f"{task.id}:summary",
            )
            task.summary = summary
            task.finished_at = utc_now()
            await self._set_status(task, "done")
            return task
        except asyncio.CancelledError:
            await self.mark_cancelled(task, "Task execution cancelled")
            raise
        except TaskCancelledByUser:
            logger.info("Task %s stopped after explicit user cancellation", task.id)
            return await self.database.get_task(task.id) or task
        except Exception as exc:
            if await self._is_cancelled_by_user(task):
                logger.info(
                    "Task %s failure ignored because the user cancelled it", task.id
                )
                return await self.database.get_task(task.id) or task
            logger.exception("Task %s failed", task.id)
            task.status = "failed"
            task.finished_at = utc_now()
            try:
                await self.database.save_task(task)
                await self._message(task.id, "system", "human", "error", str(exc))
                await self._publish(
                    Event(task_id=task.id, type="task_update", payload=task.model_dump(mode="json"))
                )
            except Exception:
                logger.exception(
                    "Failed to persist failure state for task %s after its original error",
                    task.id,
                )
            raise

    async def mark_cancelled(self, task: Task, reason: str, *, cancelled_by_user: bool = False) -> Task:
        if task.status == "cancelled":
            return task
        if not cancelled_by_user and task.status in {"planning", "running", "review"}:
            task.interrupted_status = task.status
        else:
            task.interrupted_status = None
        task.status = "cancelled"
        task.finished_at = utc_now()
        task.pending_question = None
        task.cancelled_by_user = cancelled_by_user
        # Preserve cancelled step state for resume
        if task.cancelled_step_index is not None:
            # Store the cancelled state in waiting fields for consistency with resume logic
            task.waiting_step_index = task.cancelled_step_index
            task.waiting_agent_id = await self._get_agent_id_for_step(task, task.cancelled_step_index)
        await self.database.save_task(task)
        await self._message(task.id, "system", "human", "system", reason)
        await self._publish(
            Event(task_id=task.id, type="task_update", payload=task.model_dump(mode="json"))
        )
        return task

    async def cancel_by_user(self, task: Task, reason: str = "Task cancelled by user") -> Task:
        """Cancel a task on explicit user request.

        A running workflow records its interruption with ``cancelled_by_user=False``
        when its coroutine is cancelled, so the task may already be ``cancelled`` by
        the time the request observes it. Explicit cancellation must always win,
        otherwise startup recovery would resume the task.
        """
        if task.status != "cancelled":
            return await self.mark_cancelled(task, reason, cancelled_by_user=True)
        if task.cancelled_by_user and task.interrupted_status is None:
            return task
        task.cancelled_by_user = True
        task.interrupted_status = None
        await self.database.save_task(task)
        await self._publish(
            Event(task_id=task.id, type="task_update", payload=task.model_dump(mode="json"))
        )
        return task

    async def submit_answer(self, task_id: str, answer: str) -> Task:
        async with self._answer_lock:
            task = await self.database.get_task(task_id)
            if task is None:
                raise KeyError("Task not found")
            pending = self._human_waiters.get(task_id)
            if task.status != "waiting_input":
                raise ValueError("This task is not waiting for an answer")
            if pending is None or pending[0].done():
                task = await self._resume_waiting_task(task)
                pending = self._human_waiters.get(task_id)
                if pending is None or pending[0].done():
                    raise ValueError(
                        "No active workflow is waiting for input; restart this task from its original instruction"
                    )

            answer = answer.strip()
            if not answer:
                raise ValueError("Answer cannot be blank")
            waiter, receiver = pending
            await self._message(task_id, "human", receiver, "response", answer)
            task.pending_question = None
            task.waiting_step_index = None
            task.waiting_agent_id = None
            await self._set_status(task, "running")
            waiter.set_result(answer)
            return task

    async def _resume_waiting_task(self, task: Task) -> Task:
        if task.status != "waiting_input":
            return task
        if task.waiting_step_index is None or task.waiting_agent_id is None:
            return task

        steps = await self.database.list_steps(task.id)
        step = next((s for s in steps if s.index == task.waiting_step_index), None)
        if step is None:
            return task

        agent = await self.database.get_agent(task.waiting_agent_id)
        if agent is None:
            return task

        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[str] = loop.create_future()
        self._human_waiters[task.id] = (waiter, task.waiting_agent_id)

        async def resume_execution() -> None:
            try:
                orchestrator_config = await self._get_orchestrator()
                planner = AgentRuntime(
                    orchestrator_config,
                    create_provider(
                        orchestrator_config.provider,
                        model=orchestrator_config.model,
                        settings=self.settings,
                    ),
                    timeout_seconds=self.settings.timeout_seconds,
                    max_retries=self.settings.max_retries,
                )

                answer = await waiter
                context_parts = [f"Original request: {task.instruction}"]
                for s in steps:
                    if s.index < step.index and s.result:
                        context_parts.append(f"{s.agent_id}: {s.result}")
                context_parts.append(f"Human answer: {answer}")
                context = "\n".join(context_parts)

                worker = AgentRuntime(
                    agent,
                    create_provider(agent.provider, model=agent.model, settings=self.settings),
                    timeout_seconds=self.settings.timeout_seconds,
                    max_retries=self.settings.max_retries,
                )

                result = await self._stream_message(
                    task.id,
                    agent.id,
                    orchestrator_config.id,
                    "response",
                    worker,
                    step.description,
                    context=context,
                    suppress_prefix="[NEEDS_INPUT]:",
                    message_id=self._step_message_id(task.id, step.index, "response"),
                )

                results = []
                for s in steps:
                    if s.index < step.index and s.result:
                        results.append(f"{s.agent_id}: {s.result}")
                results.append(f"{agent.name}: {result}")

                step.status = "done"
                step.result = result
                await self.database.save_step(step)
                await self._publish(
                    Event(task_id=task.id, type="step_update", payload=step.model_dump(mode="json"))
                )

                remaining_steps = [s for s in steps if s.index > step.index]
                for next_step in remaining_steps:
                    next_agent = await self.database.get_agent(next_step.agent_id)
                    if next_agent is None or not next_agent.enabled or next_agent.role != "worker":
                        raise ValueError(f"Plan assigned an unavailable worker: {next_step.agent_id}")
                    if next_step.status == "done" and next_step.result:
                        # Recorded before the interruption: adopt the stored result
                        # instead of invoking the provider a second time.
                        results.append(f"{next_agent.name}: {next_step.result}")
                        continue

                    next_step.status = "in_progress"
                    await self.database.save_step(next_step)
                    await self._publish(
                        Event(task_id=task.id, type="step_update", payload=next_step.model_dump(mode="json"))
                    )
                    await self._message(
                        task.id,
                        orchestrator_config.id,
                        next_agent.id,
                        "assignment",
                        next_step.description,
                        message_id=self._step_message_id(task.id, next_step.index, "assignment"),
                    )
                    next_worker = AgentRuntime(
                        next_agent,
                        create_provider(next_agent.provider, model=next_agent.model, settings=self.settings),
                        timeout_seconds=self.settings.timeout_seconds,
                        max_retries=self.settings.max_retries,
                    )
                    next_context = f"Original request: {task.instruction}\nEarlier results:\n" + "\n".join(results)
                    next_result = await self._stream_message(
                        task.id,
                        next_agent.id,
                        orchestrator_config.id,
                        "response",
                        next_worker,
                        next_step.description,
                        context=next_context,
                        suppress_prefix="[NEEDS_INPUT]:",
                        message_id=self._step_message_id(task.id, next_step.index, "response"),
                    )
                    results.append(f"{next_agent.name}: {next_result}")
                    next_step.status = "done"
                    next_step.result = next_result
                    await self.database.save_step(next_step)
                    await self._publish(
                        Event(task_id=task.id, type="step_update", payload=next_step.model_dump(mode="json"))
                    )

                await self._set_status(task, "review")
                summary = await self._stream_message(
                    task.id,
                    orchestrator_config.id,
                    "human",
                    "summary",
                    planner,
                    "Write the final summary for the human using the completed work below. "
                    "Be clear and do not claim facts not present in the work.",
                    context="\n\n".join(results),
                    message_id=f"{task.id}:summary",
                )
                task.summary = summary
                task.finished_at = utc_now()
                await self._set_status(task, "done")
            except asyncio.CancelledError:
                await self.mark_cancelled(task, "Task execution cancelled")
                raise
            except TaskCancelledByUser:
                logger.info("Task %s stopped after explicit user cancellation", task.id)
                return
            except Exception as exc:
                if await self._is_cancelled_by_user(task):
                    logger.info(
                        "Task %s failure ignored because the user cancelled it", task.id
                    )
                    return
                logger.exception("Task %s failed during resume", task.id)
                task.status = "failed"
                task.finished_at = utc_now()
                try:
                    await self.database.save_task(task)
                    await self._message(task.id, "system", "human", "error", str(exc))
                    await self._publish(
                        Event(task_id=task.id, type="task_update", payload=task.model_dump(mode="json"))
                    )
                except Exception:
                    logger.exception(
                        "Failed to persist failure state for task %s after its original error",
                        task.id,
                    )
            finally:
                pending = self._human_waiters.get(task.id)
                if pending is not None and pending[0] is waiter:
                    del self._human_waiters[task.id]

        self._spawn(task.id, resume_execution())
        return task

    async def resume_interrupted_task(self, task: Task) -> None:
        """Recover interrupted work, always ending in a terminal status.

        Recovery calls providers, so it can fail like any other workflow. Leaving the
        task non-terminal would strand it in ``running``/``review`` with an
        ``in_progress`` step and no error surfaced through the API, so a raised error
        is persisted as ``failed`` (matching ``run()`` and the human-answer resume path).
        """
        try:
            await self._recover_interrupted_task(task)
        except TaskCancelledByUser:
            logger.info("Task %s stopped after explicit user cancellation", task.id)
        except Exception as exc:
            await self._record_recovery_failure(task, exc)

    async def _record_recovery_failure(self, task: Task, exc: Exception) -> None:
        """Persist a terminal failure for a recovered workflow that raised."""
        if await self._is_cancelled_by_user(task):
            logger.info("Task %s failure ignored because the user cancelled it", task.id)
            return
        logger.error("Task %s failed during recovery", task.id, exc_info=exc)
        task.status = "failed"
        task.finished_at = utc_now()
        try:
            await self.database.save_task(task)
            await self._message(task.id, "system", "human", "error", str(exc))
            await self._publish(
                Event(task_id=task.id, type="task_update", payload=task.model_dump(mode="json"))
            )
        except Exception:
            logger.exception(
                "Failed to persist failure state for task %s after its original error",
                task.id,
            )

    async def _recover_interrupted_task(self, task: Task) -> None:
        if task.cancelled_by_user:
            return
        if task.interrupted_status == "planning":
            await self.database.reset_plan_steps(task.id)
            task.interrupted_status = None
            task.cancelled_step_index = None
            task.cancelled_context = None
            task.cancelled_by_user = False
            task.status = "queued"
            task.finished_at = None
            await self.database.save_task(task)
            await self.run(task)
            return

        if task.interrupted_status == "review":
            task.cancelled_by_user = False
            task.interrupted_status = None
            task.finished_at = None
            await self._set_status(task, "review")
            orchestrator_config = await self._get_orchestrator()
            planner = AgentRuntime(
                orchestrator_config,
                create_provider(
                    orchestrator_config.provider,
                    model=orchestrator_config.model,
                    settings=self.settings,
                ),
                timeout_seconds=self.settings.timeout_seconds,
                max_retries=self.settings.max_retries,
            )
            results = []
            for step in await self.database.list_steps(task.id):
                if step.result:
                    results.append(f"{step.agent_id}: {step.result}")
            summary = await self._stream_message(
                task.id,
                orchestrator_config.id,
                "human",
                "summary",
                planner,
                "Write the final summary for the human using the completed work below. "
                "Be clear and do not claim facts not present in the work.",
                context="\n\n".join(results),
                message_id=f"{task.id}:summary",
            )
            task.summary = summary
            task.finished_at = utc_now()
            await self._set_status(task, "done")
            return

        steps = await self.database.list_steps(task.id)
        if not steps:
            await self.database.reset_plan_steps(task.id)
            task.interrupted_status = None
            task.cancelled_step_index = None
            task.cancelled_context = None
            task.status = "queued"
            task.finished_at = None
            await self.database.save_task(task)
            await self.run(task)
            return
        if task.cancelled_step_index is None:
            await self.database.reset_plan_steps(task.id)
            task.cancelled_by_user = False
            task.interrupted_status = None
            task.status = "queued"
            task.finished_at = None
            await self.database.save_task(task)
            await self.run(task)
            return
        await self._resume_cancelled_task(task)

    async def _resume_cancelled_task(self, task: Task) -> None:
        if task.status != "cancelled":
            return
        if await self._is_cancelled_by_user(task):
            return
        task.cancelled_by_user = False
        task.interrupted_status = None
        task.finished_at = None
        await self._set_status(task, "running")
        if task.cancelled_step_index is None:
            return

        steps = await self.database.list_steps(task.id)
        step = next((s for s in steps if s.index == task.cancelled_step_index), None)
        if step is None:
            return

        agent = await self.database.get_agent(step.agent_id)
        if agent is None:
            return

        # Get previous results
        results = []
        for s in steps:
            if s.index < step.index and s.result:
                results.append(f"{s.agent_id}: {s.result}")
        
        # Get orchestrator
        orchestrator_config = await self._get_orchestrator()
        planner = AgentRuntime(
            orchestrator_config,
            create_provider(
                orchestrator_config.provider,
                model=orchestrator_config.model,
                settings=self.settings,
            ),
            timeout_seconds=self.settings.timeout_seconds,
            max_retries=self.settings.max_retries,
        )

        # Resume from cancelled step
        worker = AgentRuntime(
            agent,
            create_provider(agent.provider, model=agent.model, settings=self.settings),
            timeout_seconds=self.settings.timeout_seconds,
            max_retries=self.settings.max_retries,
        )

        # Use stored context or build from results
        context = task.cancelled_context or f"Original request: {task.instruction}\nEarlier results:\n" + "\n".join(results)

        if step.status == "done" and step.result:
            # The interrupted step had already recorded its result before the shutdown,
            # so recovery adopts it instead of invoking the provider a second time.
            result = step.result
        else:
            result = await self._stream_message(
                task.id,
                agent.id,
                orchestrator_config.id,
                "response",
                worker,
                step.description,
                context=context,
                suppress_prefix="[NEEDS_INPUT]:",
                message_id=self._step_message_id(task.id, step.index, "response"),
            )
            step.status = "done"
            step.result = result
            await self.database.save_step(step)
            await self._publish(
                Event(task_id=task.id, type="step_update", payload=step.model_dump(mode="json"))
            )

        results.append(f"{agent.name}: {result}")

        # Process remaining steps
        remaining_steps = [s for s in steps if s.index > step.index]
        for next_step in remaining_steps:
            next_agent = await self.database.get_agent(next_step.agent_id)
            if next_agent is None or not next_agent.enabled or next_agent.role != "worker":
                raise ValueError(f"Plan assigned an unavailable worker: {next_step.agent_id}")
            if next_step.status == "done" and next_step.result:
                # Recorded before the interruption: adopt the stored result instead of
                # invoking the provider a second time.
                results.append(f"{next_agent.name}: {next_step.result}")
                continue

            next_step.status = "in_progress"
            await self.database.save_step(next_step)
            await self._publish(
                Event(task_id=task.id, type="step_update", payload=next_step.model_dump(mode="json"))
            )
            await self._message(
                task.id,
                orchestrator_config.id,
                next_agent.id,
                "assignment",
                next_step.description,
                message_id=self._step_message_id(task.id, next_step.index, "assignment"),
            )
            next_worker = AgentRuntime(
                next_agent,
                create_provider(next_agent.provider, model=next_agent.model, settings=self.settings),
                timeout_seconds=self.settings.timeout_seconds,
                max_retries=self.settings.max_retries,
            )
            next_context = f"Original request: {task.instruction}\nEarlier results:\n" + "\n".join(results)
            next_result = await self._stream_message(
                task.id,
                next_agent.id,
                orchestrator_config.id,
                "response",
                next_worker,
                next_step.description,
                context=next_context,
                suppress_prefix="[NEEDS_INPUT]:",
                message_id=self._step_message_id(task.id, next_step.index, "response"),
            )
            results.append(f"{next_agent.name}: {next_result}")
            next_step.status = "done"
            next_step.result = next_result
            await self.database.save_step(next_step)
            await self._publish(
                Event(task_id=task.id, type="step_update", payload=next_step.model_dump(mode="json"))
            )

        await self._set_status(task, "review")
        summary = await self._stream_message(
            task.id,
            orchestrator_config.id,
            "human",
            "summary",
            planner,
            "Write the final summary for the human using the completed work below. "
            "Be clear and do not claim facts not present in the work.",
            context="\n\n".join(results),
            message_id=f"{task.id}:summary",
        )
        task.summary = summary
        task.finished_at = utc_now()
        await self._set_status(task, "done")

    async def _ask_human(
        self, task: Task, step: PlanStep, agent_id: str, question: str
    ) -> str:
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[str] = loop.create_future()
        self._human_waiters[task.id] = (waiter, agent_id)
        try:
            step.status = "needs_input"
            step.result = question
            await self.database.save_step(step)
            await self._publish(
                Event(task_id=task.id, type="step_update", payload=step.model_dump(mode="json"))
            )
            task.pending_question = question
            task.waiting_step_index = step.index
            task.waiting_agent_id = agent_id
            await self._message(task.id, agent_id, "human", "question", question)
            await self._set_status(task, "waiting_input")
            return await waiter
        finally:
            pending = self._human_waiters.get(task.id)
            if pending is not None and pending[0] is waiter:
                del self._human_waiters[task.id]

    async def _make_plan(self, planner: AgentRuntime, task: Task) -> TaskPlan:
        available = [
            agent for agent in await self.database.list_agents()
            if agent.role == "worker" and agent.enabled
        ]
        if not available:
            raise ValueError("At least one enabled worker agent is required")
        requested = await planner.respond(
            "Create a small execution plan for the human request. Assign each step to one "
            "of the available worker IDs. Return only valid JSON with this shape: "
            '{"steps":[{"description":"...","agent_id":"..."}]}. '
            "Return only valid JSON; do not include markdown.",
            context=(
                f"Request: {task.instruction}\n"
                f"Available workers: {json.dumps([a.id for a in available])}\n"
                f"Maximum steps: {self.settings.max_steps}"
            ),
        )
        try:
            plan = TaskPlan.model_validate_json(requested)
        except ValidationError as exc:
            raise ValueError("Orchestrator returned an invalid task plan") from exc
        if len(plan.steps) > self.settings.max_steps:
            raise ValueError(f"Plan exceeds the maximum of {self.settings.max_steps} steps")
        allowed = {agent.id for agent in available}
        invalid = [step.agent_id for step in plan.steps if step.agent_id not in allowed]
        if invalid:
            raise ValueError(f"Plan references unavailable worker(s): {', '.join(invalid)}")
        return plan

    async def _get_orchestrator(self):
        agents = await self.database.list_agents()
        for agent in agents:
            if agent.role == "orchestrator" and agent.enabled:
                return agent
        raise ValueError("No enabled orchestrator agent is configured")

    async def _get_agent_id_for_step(self, task: Task, step_index: int) -> str | None:
        steps = await self.database.list_steps(task.id)
        for step in steps:
            if step.index == step_index:
                return step.agent_id
        return None

    async def _set_status(self, task: Task, status: str) -> None:
        if status != "cancelled" and await self._is_cancelled_by_user(task):
            # An explicit user cancellation must never be overwritten by a workflow
            # that is still running (for example one that was recovered at startup).
            raise TaskCancelledByUser(task.id)
        task.status = status
        await self.database.save_task(task)
        await self._publish(
            Event(task_id=task.id, type="task_update", payload=task.model_dump(mode="json"))
        )

    async def _message(
        self,
        task_id: str,
        sender: str,
        receiver: str,
        kind: str,
        content: str,
        *,
        message_id: str | None = None,
    ) -> None:
        message = Message(
            task_id=task_id,
            sender=sender,
            receiver=receiver,
            kind=kind,
            content=content,
        )
        if message_id is not None:
            message.id = message_id
        await self.database.save_message(message)
        await self._publish(
            Event(task_id=task_id, type="message", payload=message.model_dump(mode="json"))
        )

    async def _stream_message(
        self,
        task_id: str,
        sender: str,
        receiver: str,
        kind: str,
        runtime: AgentRuntime,
        instruction: str,
        *,
        context: str = "",
        suppress_prefix: str | None = None,
        message_id: str | None = None,
    ) -> str:
        message = Message(
            task_id=task_id,
            sender=sender,
            receiver=receiver,
            kind=kind,
            content="",
        )
        if message_id is not None:
            message.id = message_id
        started = False
        prefix_buffer = ""
        stream_enabled = True

        async def emit(chunk: str) -> None:
            nonlocal started
            if not started:
                await self.database.save_message(message)
                started = True
                await self._publish(
                    Event(
                        task_id=task_id,
                        type="message_start",
                        payload=message.model_dump(mode="json"),
                    )
                )
            message.content += chunk
            await self.database.save_message(message)
            await self._publish(
                Event(
                    task_id=task_id,
                    type="message_delta",
                    payload={
                        "id": message.id,
                        "content": chunk,
                        "sender": message.sender,
                        "receiver": message.receiver,
                        "kind": message.kind,
                        "created_at": message.created_at.isoformat(),
                    },
                )
            )

        async def on_chunk(chunk: str) -> None:
            nonlocal prefix_buffer, stream_enabled
            if suppress_prefix is None or not stream_enabled:
                if stream_enabled:
                    await emit(chunk)
                return
            prefix_buffer += chunk
            normalized = prefix_buffer.lstrip()
            if suppress_prefix.startswith(normalized):
                return
            if normalized.startswith(suppress_prefix):
                stream_enabled = False
                prefix_buffer = ""
                return
            await emit(prefix_buffer)
            prefix_buffer = ""

        async def on_retry() -> None:
            nonlocal prefix_buffer, stream_enabled
            prefix_buffer = ""
            stream_enabled = True
            if started:
                message.content = ""
                await self.database.save_message(message)
                await self._publish(
                    Event(
                        task_id=task_id,
                        type="message_reset",
                        payload={"id": message.id},
                    )
                )

        result = await runtime.respond(
            instruction,
            context=context,
            on_chunk=on_chunk,
            on_retry=on_retry,
        )
        if suppress_prefix is not None and result.strip().startswith(suppress_prefix):
            if started:
                await self._publish(
                    Event(
                        task_id=task_id,
                        type="message_discard",
                        payload={"id": message.id},
                    )
                )
            return result
        if prefix_buffer:
            await emit(prefix_buffer)
        message.content = result
        await self.database.save_message(message)
        await self._publish(
            Event(task_id=task_id, type="message", payload=message.model_dump(mode="json"))
        )
        return result

    async def _publish(self, event: Event) -> None:
        await self.database.save_event(event)
        await self.bus.publish(event)
