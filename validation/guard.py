"""Test-only guard installed in an ephemeral hosted virtualenv."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import urllib.request


def permitted_child(args, kwargs, state):
    if not isinstance(args, (list, tuple)):
        return False
    if list(args) != [sys.executable, "-m", "minimax_mcp.server"]:
        return False
    if kwargs.get("shell") or kwargs.get("executable"):
        return False
    env = kwargs.get("env", {})
    cwd = Path(kwargs.get("cwd") or os.getcwd()).resolve()
    return (
        state["allow_stdio"]
        and cwd == Path(state["source"]).resolve()
        and env.get("MINIMAX_API_KEY") == "test-api-key"
        and env.get("MINIMAX_API_HOST") == "https://api.example.invalid"
    )


def install():
    state = json.loads(Path(__file__).with_name("minimax_ci_guard_state.json").read_text())

    def event(kind):
        with open(state["log"], "a", encoding="utf-8") as output:
            output.write(json.dumps({"event": kind, "pid": os.getpid()}) + "\n")

    def deny(*args, **kwargs):
        event("violation")
        raise AssertionError("CI forbids real network, listeners and unapproved subprocesses")

    import dotenv
    import requests
    import httpx
    import httpx2

    dotenv.load_dotenv = lambda *args, **kwargs: False
    dotenv.find_dotenv = lambda *args, **kwargs: ""
    requests.sessions.Session.request = deny
    httpx.Client.send = deny
    httpx.AsyncClient.send = deny
    httpx2.Client.send = deny
    httpx2.AsyncClient.send = deny
    urllib.request.urlopen = deny
    socket.getaddrinfo = deny
    for method in ("connect", "connect_ex", "bind", "listen", "sendto"):
        setattr(socket.socket, method, deny)

    original_popen = subprocess.Popen.__init__

    def guarded_popen(instance, args, *positional, **kwargs):
        if not permitted_child(args, kwargs, state):
            return deny()
        event("approved_stdio_spawn")
        return original_popen(instance, args, *positional, **kwargs)

    subprocess.Popen.__init__ = guarded_popen
    event("activated")
