import asyncio
import os
import shlex
from pathlib import Path

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
            
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120.0)
            exit_code = proc.returncode
            
            output = stdout.decode('utf-8', errors='replace')
            errors = stderr.decode('utf-8', errors='replace')
            
            combined_output = output + errors
            return exit_code, combined_output if combined_output.strip() else "[Command executed with empty output]"
            
        except asyncio.TimeoutError:
            return -1, "Execution terminated: Command breached time allocation window."
        except Exception as system_fault:
            return -2, f"Subprocess layer structural fault: {system_fault}"

shell_engine = PersistentTmuxShell()