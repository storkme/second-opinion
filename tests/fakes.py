"""Shared test fakes."""


class FakeProc:
    """Stand-in for subprocess.CompletedProcess — only the fields the code reads."""
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
