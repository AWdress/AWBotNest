import asyncio
from libs.log import logger
import traceback


async def bash(*command):
    command_list = command[0].split(" ") if len(command) == 1 else command
    try:
        proc = await asyncio.create_subprocess_exec(
            *command_list,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout = "\n".join(
            list(
                filter(
                    None, map(lambda x: x.decode().strip(), await proc.communicate())
                )
            )
        )
        if proc.returncode != 0:
            raise RuntimeError(f"bash {command} failed: {stdout}")
        return stdout
    except Exception as e:
        logger.error(traceback.format_exc())