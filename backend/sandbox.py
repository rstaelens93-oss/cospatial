import sys
import json
import traceback
import multiprocessing
from io import StringIO


def execute_sandbox(code: str, q: multiprocessing.Queue) -> None:
    """Run code in a restricted namespace and put the result on the queue."""
    old_stdout = sys.stdout
    sys.stdout = StringIO()

    # Restrict builtins to a safe math/data subset only.
    safe_builtins = {
        b: getattr(__builtins__, b)
        for b in [
            "abs", "all", "any", "bool", "dict", "enumerate", "float",
            "int", "len", "list", "map", "max", "min", "pow", "print",
            "range", "round", "set", "str", "sum", "zip",
        ]
    }
    safe_globals = {
        "__builtins__": safe_builtins,
        "math": __import__("math"),
        "json": __import__("json"),
    }

    try:
        exec(compile(code, "<math>", "exec"), safe_globals, {})
        output = sys.stdout.getvalue().strip()
        q.put({
            "success": True,
            "data": json.loads(output) if output else None,
            "error": None,
        })
    except Exception:
        q.put({"success": False, "data": None, "error": traceback.format_exc()})
    finally:
        sys.stdout = old_stdout


def execute_user_spatial_math(code: str, timeout: float = 3.0) -> dict:
    """
    Execute ``code`` in an isolated subprocess with a hard timeout.

    Returns a dict with keys: success (bool), data (any), error (str | None).
    """
    q: multiprocessing.Queue = multiprocessing.Queue()
    p = multiprocessing.Process(target=execute_sandbox, args=(code, q))
    p.start()
    p.join(timeout)

    if p.is_alive():
        p.terminate()
        p.join()
        return {"success": False, "data": None, "error": f"Timeout after {timeout}s"}

    return q.get()
