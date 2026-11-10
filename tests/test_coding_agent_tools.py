from types import SimpleNamespace as NS

from coding_agent_app.tools import build_show_changes


class RecordingSandbox:
    """Stands in for the sandbox: records the commands it is asked to run."""

    def __init__(self):
        self.commands = []

    def run(self, command):
        self.commands.append(command)
        return NS(stdout="the diff")


def test_show_changes_stages_everything_and_diffs_against_the_start_commit():
    """The reviewer's diff includes new and edited files, because show_changes stages everything before diffing against the sandbox start commit."""
    sandbox = RecordingSandbox()
    assert build_show_changes(sandbox, "/work", "abc123").invoke({}) == "the diff"
    assert sandbox.commands == ["git -C /work add -A", "git -C /work diff --cached abc123"]
