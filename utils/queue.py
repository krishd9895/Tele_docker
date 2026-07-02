import asyncio
import logging

class CommandSerializingQueue:
    def __init__(self):
        self._lock = asyncio.Lock()

    async def execute(self, coroutine_task):
        async with self._lock:
            try:
                return await coroutine_task
            except Exception as e:
                logging.error(f"Execution engine tracking failure: {e}")
                raise e

global_execution_queue = CommandSerializingQueue()