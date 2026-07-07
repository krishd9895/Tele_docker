import asyncio
import os
import shlex
from pathlib import Path

# Hard cap on output collected from any command
MAX_OUTPUT_BYTES = 512 * 1024   # 512 KB — beyond this we truncate
# Telegram message limit (with HTML wrapper overhead)
TELEGRAM_MSG_LIMIT = 3800


class PersistentTmuxShell:
    def __init__(self):
        self.current_wd = os.path.expanduser("~")

    async def execute_cmd(self, raw_cmd: str) -> tuple[int, str]:
        parsed_args = shlex.split(raw_cmd)
        if not parsed_args:
            return 0, ""

        if parsed_args[0] == "cd":
            try:
                target = parsed_args[1] if len(parsed_args) > 1 else os.path.expanduser("~")
                new_path = Path(self.current_wd) / target
                resolved = new_path.resolve(strict=True)
                if resolved.is_dir():
                    self.current_wd = str(resolved)
                    return 0, f"Changed working directory to: {self.current_wd}"
                else:
                    return 1, "Target directory execution paths invalid."
            except Exception as ex:
                return 1, f"Navigation system failure mapping context: {ex}"

        try:
            proc = await asyncio.create_subprocess_shell(
                raw_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.current_wd
            )

            # Read output with a hard size cap — prevents RAM exhaustion on huge output
            async def _read_capped(stream, limit: int) -> bytes:
                chunks = []
                total = 0
                try:
                    while True:
                        chunk = await asyncio.wait_for(stream.read(4096), timeout=2.0)
                        if not chunk:
                            break
                        chunks.append(chunk)
                        total += len(chunk)
                        if total >= limit:
                            # Drain the rest without storing it
                            proc.stdout and proc.stdout._transport and proc.stdout._transport.close()
                            proc.stderr and proc.stderr._transport and proc.stderr._transport.close()
                            break
                except (asyncio.TimeoutError, Exception):
                    pass
                return b"".join(chunks)

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    asyncio.gather(
                        _read_capped(proc.stdout, MAX_OUTPUT_BYTES),
                        _read_capped(proc.stderr, MAX_OUTPUT_BYTES),
                    ),
                    timeout=120.0
                )
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                return -1, "Execution terminated: Command exceeded time limit (120s)."

            exit_code = proc.returncode or 0

            stdout_str = stdout_bytes.decode('utf-8', errors='replace')
            stderr_str = stderr_bytes.decode('utf-8', errors='replace')
            combined = stdout_str + stderr_str

            # Warn if output was capped
            total_bytes = len(stdout_bytes) + len(stderr_bytes)
            if total_bytes >= MAX_OUTPUT_BYTES:
                combined = combined[:MAX_OUTPUT_BYTES].rsplit('\n', 1)[0]
                combined += f"\n\n⚠️ Output capped at {MAX_OUTPUT_BYTES // 1024}KB — use redirection to save full output:\n  command > /tmp/output.txt"

            return exit_code, combined if combined.strip() else "[Command executed with empty output]"

        except asyncio.TimeoutError:
            return -1, "Execution terminated: Command breached time allocation window."
        except Exception as system_fault:
            return -2, f"Subprocess layer structural fault: {system_fault}"


shell_engine = PersistentTmuxShell()