"""Markdown the system writes for people to read: how tall a chat answer will display, where a too-tall answer is split, the
table-size check, the approval cards, the plan view and the pull request description."""
import math
from dataclasses import dataclass

import mistune

from app.coding_task_models import CodingAgentPlan
from app.coding_task_store import get_coding_agent_task
from app.config import CODING_AGENT_DEFAULT_BASE_BRANCH, CODING_APPROVAL_CARD_TOOLS, MAX_ANSWER_TABLE_ROWS
from app.orchestrator.state import AgentState
from app.shared_helpers import get_plan_draft


PENDING_APPROVAL_MESSAGE = ("This question requires running a large query. "
                            "Approve or reject it to continue.")


# Shared GFM table parser -- used by check_table_rows & extract_table_values
markdown_ast = mistune.create_markdown(renderer="ast", plugins=["table"])


def check_table_rows(answer_markdown: str) -> bool:
    """True if every markdown table in the answer has at most
    MAX_ANSWER_TABLE_ROWS data rows."""
    for block in markdown_ast(answer_markdown):
        if block.get("type") != "table":
            continue
        body = next((c for c in block["children"] if c["type"] == "table_body"), None)
        if body is None:
            continue
        if len(body["children"]) > MAX_ANSWER_TABLE_ROWS:
            return False
    return True


# Answer height estimate, fitted to heights read from the app (app units).
MAX_ANSWER_HEIGHT = 4000
HEIGHT_PER_CHAR = 0.6
HEIGHT_PER_TABLE_ROW = 42
HEIGHT_PER_CHART = 700
HEIGHT_PER_CODE_LINE = 30
HEADING_CHAR_WEIGHT = 1.33     # headings are 32px against 24px body text


def inline_text_and_images(nodes: list[dict]) -> tuple[str, int]:
    """Visible text and image count of inline nodes."""
    text, images = "", 0
    for node in nodes:
        if node["type"] == "image":
            images += 1
        elif "children" in node:
            inner_text, inner_images = inline_text_and_images(node["children"])
            text, images = text + inner_text, images + inner_images
        else:
            text += node.get("raw", "")
    return text, images


def block_height(node: dict) -> float:
    kind = node["type"]
    if kind in ("paragraph", "block_text", "heading"):
        text, images = inline_text_and_images(node["children"])
        weight = HEADING_CHAR_WEIGHT if kind == "heading" else 1
        return HEIGHT_PER_CHAR * weight * len(text) + HEIGHT_PER_CHART * images
    if kind == "block_code":
        return HEIGHT_PER_CODE_LINE * (node["raw"].rstrip("\n").count("\n") + 1)
    if kind == "table":
        body = next((c for c in node["children"] if c["type"] == "table_body"), None)
        return HEIGHT_PER_TABLE_ROW * (1 + (len(body["children"]) if body else 0))
    return sum(block_height(child) for child in node.get("children", []))


def answer_height(answer_markdown: str) -> float:
    return sum(block_height(node) for node in markdown_ast(answer_markdown))


def block_label(node: dict) -> str:
    """What a block starts with, for naming where an answer is cut."""
    if node["type"] in ("heading", "paragraph", "block_text"):
        return inline_text_and_images(node["children"])[0][:60] or "a chart"
    return {"list": "a list", "table": "a table", "block_code": "a code block"}.get(node["type"], node["type"])


def split_answer_into_sections(answer_markdown: str) -> list[dict]:
    """Splits the answer into consecutive sections that each fit MAX_ANSWER_HEIGHT, cutting before a
    heading when one is available. Each section is {"starts_with": its first block, "height": its estimated height}."""
    block_list = []
    for markdown_node in markdown_ast(answer_markdown):
        if markdown_node["type"] == "blank_line":
            continue
        block_list.append({
            "label": block_label(markdown_node),
            "is_heading": markdown_node["type"] == "heading",
            "height": block_height(markdown_node),
        })

    answer_sections = []
    height_left = sum(block["height"] for block in block_list)
    while len(block_list) > 1 and height_left > MAX_ANSWER_HEIGHT:
        # index of the first block where the running height passes the limit
        running_height = 0
        for first_over_limit_index, block in enumerate(block_list):
            running_height += block["height"]
            if running_height > MAX_ANSWER_HEIGHT:
                break
        first_over_limit_index = max(first_over_limit_index, 1)    # a block taller than the limit gets a section to itself

        # index where the next section begins: the last heading before that block, else the block itself
        cut_index = first_over_limit_index
        for index in range(1, first_over_limit_index):
            if block_list[index]["is_heading"]:
                cut_index = index

        section_blocks = block_list[:cut_index]
        section_height = sum(block["height"] for block in section_blocks)
        answer_sections.append({"starts_with": section_blocks[0]["label"], "height": section_height})
        block_list = block_list[cut_index:]
        height_left -= section_height
    answer_sections.append({"starts_with": block_list[0]["label"], "height": height_left})

    return answer_sections


APPROVAL_QUERY_ROW_HEIGHT = 33    # 22px code at line-height 1.5
APPROVAL_QUERY_ROW_CHARS = 45     # narrow wrap width, so long lines are over-counted rather than under


def approval_message(estimated_cost: str, query: str) -> str:
    """The approval card text. Only as many query lines as fit in half the answer height are shown, so the
    card's buttons stay on screen."""
    lines = query.split("\n")
    row_budget = int(MAX_ANSWER_HEIGHT / 2 // APPROVAL_QUERY_ROW_HEIGHT)
    shown_lines, rows_used = [], 0
    is_one_long_line = False

    for line in lines:
        rows_used += max(1, math.ceil(len(line) / APPROVAL_QUERY_ROW_CHARS))
        if rows_used > row_budget:
            break
        shown_lines.append(line)

    if not shown_lines:     # the first line alone is over the budget
        is_one_long_line = True
        shown_lines = [lines[0][:row_budget * APPROVAL_QUERY_ROW_CHARS]]

    shown_query = "\n".join(shown_lines)
    message = (
        f"{PENDING_APPROVAL_MESSAGE}\n\n"
        f"Estimated cost: {estimated_cost}\n\n"
        f"```sql\n{shown_query}\n```"
    )

    trimmed = len(shown_lines) < len(lines) or is_one_long_line
    if trimmed:
        message += "\n..."
        message += "\n\nThe query is too long to display in full"
    return message


CODING_TASK_APPROVAL_MESSAGE = ("The agent wants to start this coding task. Approve or reject it to continue. "
                                "To read the full plan, select the coding plan icon in the top right.")
PULL_REQUEST_APPROVAL_MESSAGE = "The agent wants to open a pull request. Approve or reject it to continue."


async def coding_approval_message(state: AgentState) -> str:
    """The approval card for the start_coding_task or open_pull_request call the turn paused on, built from what is
    stored: the saved plan's name and goal, or the finished task's name and branch."""
    pending_call = next(tc for tc in state["messages"][-1].tool_calls if tc["name"] in CODING_APPROVAL_CARD_TOOLS)

    # Starting a coding task: the saved plan's name and goal, read from the draft the task will start from
    if pending_call["name"] == "start_coding_task":
        draft = await get_plan_draft(state["conversation_id"])
        plan = CodingAgentPlan.model_validate(draft["plan"])
        return f"{CODING_TASK_APPROVAL_MESSAGE}\n\n### {plan.name}\n\n**Goal:** {plan.goal}"

    # Opening a pull request: the finished task's name and the branch that goes into dev
    task = await get_coding_agent_task(pending_call["args"]["task_id"])
    return (f"{PULL_REQUEST_APPROVAL_MESSAGE}\n\n### {task['plan']['name']}\n\n"
            f"`{task['coding_agent_result']['branch']}` into `{CODING_AGENT_DEFAULT_BASE_BRANCH}`")


def markdown_bullets(items: list[str]) -> str:
    """A markdown bullet list, one line per item; empty when there are none."""
    return "\n".join(f"- {item}" for item in items)


def numbered_markdown_list(items: list[str]) -> str:
    """A markdown numbered list, one line per item; empty when there are none."""
    return "\n".join(f"{number}. {item}" for number, item in enumerate(items, start=1))


@dataclass
class PlanMarkdown:
    """Every part of the plan view, in the order it displays. A part left empty is left out."""
    title: str = ""
    goal: str = ""
    branch_and_reviewer: str = ""
    requirements: str = ""
    constraints: str = ""
    tests: str = ""
    decisions: str = ""
    files_to_imitate: str = ""
    suggested_code: str = ""

    def render(self) -> str:
        labeled_sections = {"Requirements": self.requirements, "Constraints": self.constraints, "Tests": self.tests,
                            "Decisions": self.decisions, "Files to imitate": self.files_to_imitate,
                            "Suggested code": self.suggested_code}
        parts = [self.title, self.goal, self.branch_and_reviewer,
                 *[f"**{label}**\n\n{text}" for label, text in labeled_sections.items() if text]]
        return "\n\n".join(parts)


def render_plan_markdown(plan: CodingAgentPlan) -> str:
    """A coding plan as markdown, every section it has."""
    markdown = PlanMarkdown()

    # Title, goal, base branch and reviewer: always present
    markdown.title = f"### {plan.name}"
    markdown.goal = f"**Goal:** {plan.goal}"
    markdown.branch_and_reviewer = f"**Base branch:** `{plan.base_branch}`, **Reviewer:** {'on' if plan.review else 'off'}"

    # Requirements, constraints, tests and decisions: one line each
    markdown.requirements = numbered_markdown_list(plan.requirements)
    markdown.constraints = markdown_bullets(plan.constraints)
    markdown.tests = markdown_bullets(plan.tests)
    markdown.decisions = markdown_bullets(plan.decisions)

    # Files to imitate: the path and what to follow in it
    markdown.files_to_imitate = markdown_bullets([f"`{file.path}`: {file.what}" for file in plan.files_to_imitate])

    # Suggested code: its purpose and target file, then the code in a block
    markdown.suggested_code = "\n\n".join(
        f"{item.purpose}{f' (`{item.file}`)' if item.file else ''}\n\n```\n{item.code}\n```" for item in plan.suggested_code)
    return markdown.render()


@dataclass
class PullRequestBody:
    """Every part of the pull request description, in the order it displays. A part left empty is left out."""
    summary: str = ""
    requirements: str = ""
    changes: str = ""
    checks: str = ""
    reviewer_agent: str = ""
    coding_agent_report: str = ""
    assumptions: str = ""
    follow_up_questions: str = ""
    footer: str = ""

    def render(self) -> str:
        headed_sections = {"Requirements": self.requirements, "Changes": self.changes, "Checks": self.checks, "Reviewer agent": self.reviewer_agent,
                           "Coding agent's report": self.coding_agent_report, "Assumptions": self.assumptions,
                           "Follow-up questions": self.follow_up_questions}
        parts = [self.summary, *[f"## {heading}\n{text.strip()}" for heading, text in headed_sections.items() if text], self.footer]
        return "\n\n".join(parts)


def build_pull_request_body(task: dict) -> str:
    """The description of the pull request, built only from the task's stored plan and result."""
    result = task["coding_agent_result"]
    review = result["review"]
    passed_checks = sum(check["passed"] for check in result["checks"])
    body = PullRequestBody()

    # Summary: the goal and the outcome, with no heading
    body.summary = (f"**Goal:** {task['plan']['goal']}\n\n"
                    f"**Outcome:** {result['outcome']}, {passed_checks} of {len(result['checks'])} checks passed")

    # Requirements: what the plan asked for, one line each
    body.requirements = numbered_markdown_list(task["plan"]["requirements"])

    # Changes: one line per changed file
    body.changes = markdown_bullets([f"`{change['path']}` (+{change['added']} -{change['removed']})" for change in result["files"]])

    # Checks: passed or failed for each, with the output of a failed one
    check_lines = []
    for check in result["checks"]:
        check_lines.append(f"- {'passed' if check['passed'] else 'FAILED'}: {check['name']}")
        if check["detail"]:
            check_lines.append(f"```\n{check['detail']}\n```")
    body.checks = "\n".join(check_lines)

    # Reviewer agent: its verdict and the criteria it failed, only when it ran
    if review["ran"]:
        failed_criteria = [f"{criterion['criterion']}: {criterion['gap']}"
                           for criterion in review["criteria"] if not criterion["passed"]]
        body.reviewer_agent = "\n".join([f"Verdict: {review['verdict']}. {review['explanation']}", markdown_bullets(failed_criteria)])

    # Coding agent's report
    body.coding_agent_report = result["report"]

    # Assumptions and follow-up questions: empty, and so left out, when there are none
    body.assumptions = markdown_bullets(result["assumptions"])
    body.follow_up_questions = markdown_bullets(result["follow_up_questions"])

    # Footer
    body.footer = f"---\nOpened from coding task `{task['task_id']}`."
    return body.render()


def render_task_plan_markdown(task_plan: dict) -> str:
    """The saved task plan as markdown: a legend of the status symbols, the scope, then each task numbered with its symbol."""
    task_status_display = {"todo": ("⬜", "To do"), "in_progress": ("🔄", "In progress"), "done": ("✅", "Done")}
    tasks = [f"{task_status_display[task['status']][0]} {task['text']}" for task in task_plan["tasks"]]

    parts = [" · ".join(f"{symbol} {label}" for symbol, label in task_status_display.values())]
    if task_plan.get("scope"):
        parts.append(f"**Scope:** {task_plan['scope']}")
    parts.append(numbered_markdown_list(tasks))
    return "\n\n".join(parts)
