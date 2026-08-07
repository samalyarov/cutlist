import subprocess

STDERR_TAIL_LINES = 15


class ToolError(RuntimeError):
    """An external tool failed, or could not be found."""


def run(cmd: list[str], *, timeout: int = 600) -> str:
    """Run a command and return its stdout.

    ffmpeg puts everything interesting on stderr and often fails deep into a
    long filtergraph, so failures report the command plus the tail of stderr
    rather than a bare exit code.
    """
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise ToolError(f"{cmd[0]} not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"{cmd[0]} timed out after {timeout}s") from exc

    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-STDERR_TAIL_LINES:])
        raise ToolError(f"{' '.join(cmd)}\nexited {proc.returncode}\n{tail}")
    return proc.stdout
