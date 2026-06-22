import asyncio
from pathlib import Path
import re

import pytest

from app import runtime_config
from app.config import Settings
from app.models import JobState
from app.pipeline import orchestrator
from app.pipeline.workflow import coerce_user_intent
from app.storage import Storage


REPO_ROOT = Path(__file__).resolve().parents[3]


def _numbered_prompt_demands() -> list[tuple[int, str]]:
    text = (REPO_ROOT / "prompts.md").read_text(encoding="utf-8")
    matches = re.findall(
        r"(?ms)^\s*(\d+)\.\s+(.*?)(?=\n\s*\d+\.|\Z)",
        text,
    )
    return [(int(number), re.sub(r"\s+", " ", body).strip()) for number, body in matches]


DEMAND_EXPECTATIONS = {
    1: {
        "document_kind": "proposal_report",
        "requirements": {
            "formal_academic_style",
            "supervisor_submission",
            "clear_title",
            "main_text",
        },
        "sample": "Graduation Project Proposal\n\nResearch background and planned contribution.",
    },
    2: {
        "document_kind": "undergraduate_thesis",
        "requirements": {
            "cover_page",
            "abstract",
            "keywords",
            "table_of_contents",
            "main_text",
            "references",
            "acknowledgements",
        },
        "sample": (
            "Undergraduate Thesis\n\n"
            "Cover Page\n\n"
            "Abstract This study evaluates a local PDF workflow.\n\n"
            "Keywords translation; layout\n\n"
            "Table of Contents\n\n"
            "Chapter 1 Introduction\n\n"
            "This is the main text of the thesis.\n\n"
            "References\n\n"
            "[1] Example reference.\n\n"
            "Acknowledgements\n\n"
            "Thanks to the supervisor."
        ),
    },
    3: {
        "document_kind": "course_paper",
        "requirements": {
            "academic_style",
            "clear_title",
            "readable_body_text",
            "consistent_citations_references",
            "references",
        },
        "sample": (
            "Final Course Paper\n\n"
            "Introduction\n\n"
            "This paper cites prior work [1] and keeps body text readable.\n\n"
            "References\n\n"
            "[1] Example source."
        ),
    },
    4: {
        "document_kind": "undergraduate_thesis",
        "requirements": {
            "main_text_12pt_simsun",
            "line_spacing_1_5",
            "level1_heading_16pt_simhei",
            "main_text_page_numbers",
        },
        "sample": (
            "University Thesis\n\n"
            "Chapter 1 Introduction\n\n"
            "This main text should use the requested university style."
        ),
    },
    5: {
        "document_kind": "undergraduate_thesis",
        "requirements": {
            "asset_alignment",
            "figure_numbering",
            "table_numbering",
            "formula_numbering",
            "list_of_figures",
            "list_of_tables",
        },
        "sample": (
            "Thesis With Figures Tables and Equations\n\n"
            "Figure 1 Example figure caption\n\n"
            "Table 1 Example table caption\n\n"
            "Equation (1)\n\n"
            "The paper discusses figures, tables, and equations."
        ),
    },
    6: {
        "document_kind": "homework",
        "requirements": {
            "formal_assignment_style",
            "cover_page",
            "course_name",
            "student_name",
            "student_id",
            "submission_date",
        },
        "sample": (
            "Cover Page\n\n"
            "Course Name: Chinese Language\n\n"
            "Student Name: Li Hua\n\n"
            "Student ID: 2024001\n\n"
            "Submission Date: 2026-06-22\n\n"
            "Assignment body text."
        ),
    },
    7: {
        "document_kind": "lab_report",
        "requirements": {
            "lab_objective",
            "lab_principles",
            "lab_procedure",
            "lab_results",
            "lab_analysis",
            "lab_conclusion",
        },
        "sample": (
            "Laboratory Report\n\n"
            "Objective\n\n"
            "Principles\n\n"
            "Procedure\n\n"
            "Results\n\n"
            "Analysis\n\n"
            "Conclusion"
        ),
    },
    8: {
        "document_kind": "book_report",
        "requirements": {
            "clear_headings",
            "highlight_important_content",
            "non_promotional_tone",
        },
        "sample": (
            "Book Report\n\n"
            "Overview\n\n"
            "Important content: the argument and evidence are highlighted.\n\n"
            "Reflection."
        ),
    },
    9: {
        "document_kind": "social_practice_report",
        "requirements": {
            "target_length_5000_words",
            "cover_page",
            "table_of_contents",
            "section_headings",
            "headers",
            "page_numbers",
        },
        "sample": (
            "Social Practice Report\n\n"
            "Cover Page\n\n"
            "Table of Contents\n\n"
            "Field Research\n\n"
            "This social practice report body records observations."
        ),
    },
    10: {
        "document_kind": "group_assignment",
        "requirements": {
            "cover_page",
            "course_name",
            "project_title",
            "group_members",
        },
        "sample": (
            "Cover Page\n\n"
            "Course Name: Software Engineering\n\n"
            "Project Title: Local Typesetting Workflow\n\n"
            "Group Members: A, B, C\n\n"
            "Project body text."
        ),
    },
}


PROMPT_DEMANDS = _numbered_prompt_demands()


@pytest.mark.parametrize(
    ("demand_number", "prompt"),
    PROMPT_DEMANDS,
    ids=[f"demand-{number}" for number, _prompt in PROMPT_DEMANDS],
)
def test_prompt_demand_has_semantic_and_local_workflow_acceptance(
    demand_number: int,
    prompt: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert len(PROMPT_DEMANDS) == 10
    expectation = DEMAND_EXPECTATIONS[demand_number]
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)
    monkeypatch.setattr(
        runtime_config,
        "settings",
        Settings(openai_api_key="", openai_api_key_from_env=False),
    )
    storage.write_runtime_config(
        {
            "openai_api_key": "",
            "agent_max_repair_attempts": 0,
            "ocr_provider_order": ["deterministic"],
        }
    )

    async def fake_render_to_pdf(html: str, output_path: Path) -> Path:
        assert "<html" in html
        output_path.write_bytes(b"%PDF-1.7\n% prompt demand acceptance\n%%EOF")
        return output_path

    monkeypatch.setattr(orchestrator, "render_to_pdf", fake_render_to_pdf)
    intent = coerce_user_intent(
        "zh-CN",
        output_kind="typeset_document",
        instruction=prompt,
    )

    asyncio.run(
        orchestrator.process_text_document_job(
            f"job_prompt_{demand_number}",
            f"doc_prompt_{demand_number}",
            "prompt-demand.txt",
            str(expectation["sample"]),
            "zh-CN",
            intent,
        )
    )

    status = storage.load_status(f"job_prompt_{demand_number}")
    user_intent = storage.read_output_json(f"doc_prompt_{demand_number}", "user-intent.json")
    semantic = storage.read_output_json(f"doc_prompt_{demand_number}", "semantic-analysis.json")
    layout_plan = storage.read_output_json(f"doc_prompt_{demand_number}", "layout-intent-plan.json")
    renderer_diagnostics = storage.read_output_json(
        f"doc_prompt_{demand_number}",
        "renderer-diagnostics.json",
    )
    workflow = storage.read_output_json(f"doc_prompt_{demand_number}", "workflow-run.json")

    expected_requirements = set(expectation["requirements"])
    intent_requirements = {
        requirement["requirement_id"] for requirement in user_intent["requirements"]
    }
    semantic_requirements = {
        requirement["requirement_id"] for requirement in semantic["recognized_requirements"]
    }
    layout_requirements = {
        requirement["requirement_id"] for requirement in layout_plan["requirements"]
    }
    renderer_requirements = {
        requirement["requirement_id"]: requirement
        for requirement in renderer_diagnostics["intent_requirements"]
    }

    assert status.status == JobState.COMPLETED
    assert user_intent["task_intent"]["document_kind"] == expectation["document_kind"]
    assert expected_requirements <= intent_requirements
    assert expected_requirements <= semantic_requirements
    assert expected_requirements <= layout_requirements
    assert expected_requirements <= set(renderer_requirements)
    assert {
        requirement["status"]
        for requirement in renderer_requirements.values()
        if requirement["requirement_id"] in expected_requirements
    } <= {"satisfied", "diagnostic", "recognized"}
    assert any(
        "requirement_satisfied" in requirement["quality_flags"]
        or "requirement_diagnostic" in requirement["quality_flags"]
        or "requirement_recognized" in requirement["quality_flags"]
        for requirement in renderer_requirements.values()
        if requirement["requirement_id"] in expected_requirements
    )
    assert "semantic_recognize" in {step["name"] for step in workflow["steps"]}
    assert "render" in {step["name"] for step in workflow["steps"]}
    assert storage.preview_html_path(f"doc_prompt_{demand_number}").exists()
    assert storage.output_pdf_path(f"doc_prompt_{demand_number}").read_bytes().startswith(b"%PDF-")
