"""
Centralized Prompt Factory

This module provides the CANONICAL prompt construction for ALL pipelines.
Every caller (simple_batch.py → core/pipeline.py) MUST use these methods to
ensure identical prompts regardless of execution mode.

WHY THIS EXISTS:
- Prompts files (.md) are already shared, but the CONSTRUCTION LOGIC (variable
  injection, angle insertion) was duplicated across files.
- This factory centralizes that logic, making drift IMPOSSIBLE unless the
  factory itself is changed.

USAGE:
    from core.prompt_factory import PromptFactory
    
    # Literature search
    for angle in PromptFactory.SEARCH_ANGLES:
        prompt = PromptFactory.build_literature_search_prompt(drug, angle)
    
    # Specialist prompts
    prompt = PromptFactory.build_specialist_prompt("genetics", context)
    
    # PI prompt
    prompt = PromptFactory.build_pi_prompt(drug, context, reports)
"""

import os
import re
from typing import Dict, List, Any, Optional

# Import the existing centralized injection (keep it in prompt_builder for backwards compat)
from core.prompt_builder import inject_verified_sources, format_papers_for_prompt

# Path configuration
PROMPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "prompts")


class PromptFactory:
    """
    Canonical prompt construction for ALL pipelines.

    Every caller (simple_batch.py → core/pipeline.py) MUST use these
    methods. This ensures prompts are identical regardless of execution
    mode.
    """
    
    # =========================================================================
    # SEARCH ANGLES - THE SOURCE OF TRUTH
    # =========================================================================
    # These define the 3 angles used in literature search.
    # NEVER duplicate these definitions elsewhere.
    
    SEARCH_ANGLES: List[Dict[str, str]] = [
        {
            "name": "Mechanism",
            "instruction": "Focus heavily on **biochemistry and structural biology**. Find the original discovery paper, crystal structures (PDB), and binding affinity limits ($K_d$, $IC_{50}$)."
        },
        {
            "name": "Genetics",
            "instruction": "Focus heavily on **genetic evidence**. Find CRISPR screens, resistance mutations, and knockout studies that validate the target."
        },
        {
            "name": "Clinical",
            "instruction": "Focus heavily on **clinical and pharmacological data**. Find clinical trial results, PK/PD data, and safety/toxicity profiles."
        }
    ]
    
    # =========================================================================
    # PROMPT FILE PATHS
    # =========================================================================
    
    LITERATURE_SEARCH_PROMPT = os.path.join(PROMPTS_DIR, "literature_search_simple.md")
    GENETICS_PROMPT = os.path.join(PROMPTS_DIR, "genetics_prompt.md")
    BIOCHEMISTRY_PROMPT = os.path.join(PROMPTS_DIR, "biochemistry_prompt.md")
    BIOPHYSICS_PROMPT = os.path.join(PROMPTS_DIR, "biophysics_prompt.md")
    REVIEWER_CRITIC_PROMPT = os.path.join(PROMPTS_DIR, "reviewer_critic.md")
    REVIEWER_NEUTRAL_PROMPT = os.path.join(PROMPTS_DIR, "reviewer_neutral.md")
    REVIEWER_SELECTIVITY_PROMPT = os.path.join(PROMPTS_DIR, "reviewer_selectivity.md")
    MISSING_PAPERS_SECTION = os.path.join(PROMPTS_DIR, "missing_papers_section.md")
    
    SPECIALIST_PROMPTS = {
        "genetics": GENETICS_PROMPT,
        "biochemistry": BIOCHEMISTRY_PROMPT,
        "biophysics": BIOPHYSICS_PROMPT,
    }
    
    # =========================================================================
    # LITERATURE SEARCH PROMPTS
    # =========================================================================
    
    @classmethod
    def _load_template(cls, path: str) -> str:
        """Load a prompt template file."""
        with open(path, "r") as f:
            return f.read()
    
    @classmethod
    def build_literature_search_prompt(
        cls,
        drug_name: str,
        angle: Optional[Dict[str, str]] = None
    ) -> str:
        """
        Build a literature search prompt for a specific angle.

        Args:
            drug_name: Name of the drug to search for
            angle: Angle definition dict with 'name' and 'instruction' keys.
                   If None, builds a generic prompt without angle focus.

        Returns:
            Complete prompt string ready to send to LLM

        Example:
            >>> for angle in PromptFactory.SEARCH_ANGLES:
            ...     prompt = PromptFactory.build_literature_search_prompt("Imatinib", angle=angle)
        """
        template = cls._load_template(cls.LITERATURE_SEARCH_PROMPT)

        # Replace core placeholders
        prompt = template.replace("{{drug_name}}", drug_name)

        # Inject angle focus if provided
        if angle:
            angle_text = f"**FOCUS AREA: {angle['name']}**\n{angle['instruction']}\n"
            prompt = prompt.replace("# Objective", f"# Objective\n{angle_text}")

        return prompt

    @classmethod
    def build_all_search_prompts(cls, drug_name: str) -> List[Dict[str, Any]]:
        """
        Build all 3 literature search prompts for a drug.

        Returns:
            List of dicts with 'angle', 'prompt' keys
        """
        return [
            {
                "angle": angle,
                "prompt": cls.build_literature_search_prompt(drug_name, angle)
            }
            for angle in cls.SEARCH_ANGLES
        ]
    
    # =========================================================================
    # SPECIALIST PROMPTS
    # =========================================================================
    
    @classmethod
    def build_specialist_prompt(
        cls,
        specialist: str,
        context: Any,  # Context object
        inject_sources: bool = True
    ) -> str:
        """
        Build a specialist prompt (genetics, biochemistry, or biophysics).
        
        Args:
            specialist: One of "genetics", "biochemistry", "biophysics"
            context: Context object with drug info and papers
            inject_sources: Whether to inject verified sources (default True)
        
        Returns:
            Complete prompt string ready to send to LLM
        
        Example:
            >>> prompt = PromptFactory.build_specialist_prompt("genetics", ctx)
        """
        if specialist not in cls.SPECIALIST_PROMPTS:
            raise ValueError(f"Unknown specialist: {specialist}. Must be one of {list(cls.SPECIALIST_PROMPTS.keys())}")
        
        template = cls._load_template(cls.SPECIALIST_PROMPTS[specialist])
        
        # Inject drug name and agent context
        prompt = template.replace("{{drug_name}}", context.drug_name)
        prompt = prompt.replace("{{agent_context}}", context.get_agent_context())
        
        # Inject verified sources from database
        if inject_sources:
            prompt = inject_verified_sources(prompt, context.drug_name,
                                             getattr(context, 'job_id', None))
        
        return prompt
    
    # =========================================================================
    # PI PROMPT
    # =========================================================================
    
    PI_PERSONA = os.path.join(PROMPTS_DIR, "pi_persona.md")
    PI_DRAFT_PROMPT = os.path.join(PROMPTS_DIR, "pi_draft.md")
    PI_REVISION_PROMPT = os.path.join(PROMPTS_DIR, "pi_revision_prompt.md")
    PI_REPORT_FORMAT = os.path.join(PROMPTS_DIR, "pi_report_format.md")

    @classmethod
    def build_pi_prompt(
        cls,
        drug_name: str,
        context: Any,  # Context object
        specialist_reports: Dict[str, str],
        inject_sources: bool = True,
        **kwargs # Accept optional 'feedback'
    ) -> str:
        """
        Build the Principal Investigator (PI) synthesis prompt.
        
        Args:
            drug_name: Name of the drug
            context: Context object with drug info and papers
            specialist_reports: Dict with keys "genetics", "biochemistry", "biophysics"
            inject_sources: Whether to inject verified sources (default True)
        
        Returns:
            Complete prompt string ready to send to LLM
        """
        # Load the Base Persona
        persona_content = cls._load_template(cls.PI_PERSONA)
        
        # Determine which external wrapper to use
        feedback = kwargs.get('feedback')
        if feedback:
            # REVISION MODE: Use pi_revision_prompt.md
            template = cls._load_template(cls.PI_REVISION_PROMPT)
            
            # Handle Dictionary Feedback (Multiple Reviewers)
            if isinstance(feedback, dict):
                # Reviewer 1 (Creative)
                rev1_text = feedback.get("Reviewer #1", "No feedback from Reviewer #1.")
                template = template.replace("{{reviewer1_feedback}}", rev1_text)
                
                # Reviewer 2 (Critic)
                rev2_text = feedback.get("Reviewer #2", "No feedback from Reviewer #2.")
                template = template.replace("{{reviewer2_feedback}}", rev2_text)

                # Selectivity Reviewer (advisory on the selectivity grade only)
                rev_sel_text = feedback.get("Selectivity Reviewer",
                                            "No feedback from the Selectivity Reviewer.")
                template = template.replace("{{reviewer_selectivity_feedback}}", rev_sel_text)

                # Check if template has legacy {{feedback}} placeholder and fill it just in case
                # (though we should update the template to not use it)
                if "{{feedback}}" in template:
                    # Construct a combined string for backward compat in template
                    combined = f"## Reviewer #1\\n{rev1_text}\\n\\n## Reviewer #2\\n{rev2_text}"
                    template = template.replace("{{feedback}}", combined)
            else:
                # Handle String Feedback (Legacy / Single Reviewer)
                template = template.replace("{{feedback}}", str(feedback))
            
            # Inject Missing Papers Section (if provided)
            missing_papers_section = kwargs.get('missing_papers_section', "")
            template = template.replace("{{missing_papers_section}}", missing_papers_section)
            
            # Inject Initial Report (if provided)
            initial_report = kwargs.get('initial_report', "No previous draft provided.")
            template = template.replace("{{initial_pi_report}}", initial_report)
        else:
            # DRAFT MODE: Use pi_draft.md
            template = cls._load_template(cls.PI_DRAFT_PROMPT)
            
        # Composition: Inject Persona into the Template
        prompt = template.replace("{{pi_persona}}", persona_content)
        
        # Inject Report Format
        report_format = cls._load_template(cls.PI_REPORT_FORMAT)
        prompt = prompt.replace("{{pi_report_format}}", report_format)
        
        # Now perform standard variable substitution on the FULL prompt
        prompt = prompt.replace("{{drug_name}}", drug_name)
        # Use minimal context for PI - papers are in {{verified_sources}}, no need to duplicate
        prompt = prompt.replace("{{context_summary}}", context.get_minimal_context())
        prompt = prompt.replace("{{genetics_report}}", specialist_reports.get("genetics") or "Missing")
        prompt = prompt.replace("{{biochem_report}}", specialist_reports.get("biochemistry") or "Missing")
        prompt = prompt.replace("{{biophysics_report}}", specialist_reports.get("biophysics") or "Missing")
        
        if inject_sources:
            prompt = inject_verified_sources(prompt, drug_name,
                                             getattr(context, 'job_id', None))
            
        return prompt

    @classmethod
    def build_reviewer_prompt(
        cls,
        drug_name: str,
        context: Any, # Context object
        pi_report: str,
        inject_sources: bool = True,
        reviewer_type: str = "critic",
        compound_identity: str = "",
        specialist_reports: Dict[str, str] = None,
    ) -> str:
        """
        Build the Reviewer prompt.

        Args:
            drug_name: Name of the drug
            context: Context object
            pi_report: The draft PI report to critique
            inject_sources: Whether to inject verified sources
            reviewer_type: "critic" (Reviewer 2), "neutral" (Reviewer 1),
                or "selectivity" (Selectivity Reviewer)
            specialist_reports: Dict with keys "genetics"/"biochemistry"/"biophysics";
                used by the selectivity reviewer to see panels the specialists flagged.

        Returns:
            Complete prompt string
        """
        if reviewer_type == "neutral":
            template = cls._load_template(cls.REVIEWER_NEUTRAL_PROMPT)
        elif reviewer_type == "selectivity":
            template = cls._load_template(cls.REVIEWER_SELECTIVITY_PROMPT)
        else:
            template = cls._load_template(cls.REVIEWER_CRITIC_PROMPT)

        # Inject PI persona so reviewer understands the PI's task and standards
        pi_persona = cls._load_template(cls.PI_PERSONA)
        prompt = template.replace("{{pi_persona}}", pi_persona)

        # Replace placeholders
        # Use minimal context for Reviewer - papers are in {{verified_sources}}, no need to duplicate
        prompt = prompt.replace("{{context_summary}}", context.get_minimal_context())
        prompt = prompt.replace("{{pi_report}}", pi_report)

        # Specialist reports (selectivity reviewer only; no-op if the placeholder is absent)
        specialist_reports = specialist_reports or {}
        specialist_block = (
            f"## Genetics\n{specialist_reports.get('genetics') or 'Missing'}\n\n"
            f"## Biochemistry\n{specialist_reports.get('biochemistry') or 'Missing'}\n\n"
            f"## Biophysics\n{specialist_reports.get('biophysics') or 'Missing'}"
        )
        prompt = prompt.replace("{{specialist_reports}}", specialist_block)
        prompt = prompt.replace(
            "{{compound_identity}}",
            compound_identity or "(No independent identity resolution was available for this run.)")
        
        # Inject verified sources
        if inject_sources:
            prompt = inject_verified_sources(prompt, drug_name,
                                             getattr(context, 'job_id', None))
            
        return prompt
    

    @classmethod
    def build_missing_papers_section(cls, missing_papers_list_text: str) -> str:
        """
        Build the missing papers section from the template.
        """
        template = cls._load_template(cls.MISSING_PAPERS_SECTION)
        return template.replace("{{missing_papers_list}}", missing_papers_list_text)

    # =========================================================================
    # VALIDATION HELPERS
    # =========================================================================
    
    @classmethod
    def validate_no_unreplaced_placeholders(cls, prompt: str) -> List[str]:
        """
        Check for any unreplaced {{placeholder}} in a prompt.
        
        Returns:
            List of unreplaced placeholder names (empty if all replaced)
        """
        return re.findall(r'\{\{(\w+)\}\}', prompt)
    
    @classmethod
    def assert_prompt_complete(cls, prompt: str, context: str = ""):
        """
        Assert that a prompt has no unreplaced placeholders.
        
        Raises:
            ValueError if unreplaced placeholders are found
        """
        unreplaced = cls.validate_no_unreplaced_placeholders(prompt)
        if unreplaced:
            raise ValueError(
                f"Unreplaced placeholders in prompt{' (' + context + ')' if context else ''}: "
                f"{unreplaced}"
            )
