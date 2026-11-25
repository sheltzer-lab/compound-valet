from pydantic import BaseModel, Field
from typing import List, Optional, Any, Dict, TYPE_CHECKING

class Context(BaseModel):
    """
    Shared Context Object containing all gathered data about the target/drug.
    This prevents hallucination by being the single source of truth for agents.
    """
    drug_name: str
    target_protein: Optional[str] = None

    # The run's paper scope. While set, the run's papers are read and written in
    # job_scoped_verified_papers instead of verified_papers, so the run cannot
    # replace the papers the saved report cites until its own report is saved.
    # See core/db.py's JobScopedVerifiedPaper. A single-run batch gets one
    # automatically; None for consensus runs, which work in a scratch database.
    job_id: Optional[str] = None


    # Placeholder for raw ingested data
    relevant_papers: List[dict] = Field(default_factory=list, description="List of verified papers from database")
    chemical_properties: dict = Field(default_factory=dict, description="Physical and chemical properties from ChemBL/PubChem")
    usage_metadata: dict = Field(default_factory=dict, description="Token usage stats from LLM calls")
    
    # NEW: Paper ID mapping
    verified_paper_ids: Dict[str, dict] = Field(default_factory=dict, description="Mapping of paper IDs (e.g., 'Aspirin-Paper-1') to paper dicts")
    
    # Audit / Critic Data
    audit_log: Optional[str] = Field(None, description="Full audit report from the Literature Critic")
    citation_score: Optional[int] = Field(None, description="Correctness score of the papers")
    citation_stats: dict = Field(default_factory=dict, description="Statistics about citations (total, hallucinations, etc)")
    
    # Time Tracking
    time_stats: dict = Field(default_factory=lambda: {"total_seconds": 0.0}, description="Execution time statistics")

    def add_usage(self, usage: dict, stage: Optional[str] = None):
        """
        Aggregates token usage from an agent call into the global context usage.
        thread-safe.

        Args:
            usage: Token usage dict from LLM response.
            stage: Optional pipeline stage key for per-stage search tracking.
        """
        import threading
        if not hasattr(self, "_usage_lock"):
             object.__setattr__(self, "_usage_lock", threading.Lock())

        if not usage:
            return

        with self._usage_lock:
            # Initialize if empty
            if "prompt_tokens" not in self.usage_metadata:
                self.usage_metadata["prompt_tokens"] = 0
                self.usage_metadata["completion_tokens"] = 0
                self.usage_metadata["total_tokens"] = 0

            self.usage_metadata["prompt_tokens"] += usage.get("prompt_tokens") or 0
            self.usage_metadata["completion_tokens"] += usage.get("completion_tokens") or 0
            self.usage_metadata["total_tokens"] += usage.get("total_tokens") or 0
            self.usage_metadata["thoughts_tokens"] = (
                self.usage_metadata.get("thoughts_tokens", 0)
                + (usage.get("thoughts_tokens") or 0)
            )
            self.usage_metadata["cached_tokens"] = (
                self.usage_metadata.get("cached_tokens", 0)
                + (usage.get("cached_tokens") or 0)
            )

            search_count = usage.get("search_grounding_queries") or 0
            self.usage_metadata["search_grounding_queries"] = (
                self.usage_metadata.get("search_grounding_queries", 0)
                + search_count
            )

            # Per-stage search tracking
            if stage and search_count > 0:
                if "search_by_stage" not in self.usage_metadata:
                    self.usage_metadata["search_by_stage"] = {}
                self.usage_metadata["search_by_stage"][stage] = (
                    self.usage_metadata["search_by_stage"].get(stage, 0)
                    + search_count
                )

    def add_time(self, seconds: float):
        """
        Aggregates execution time into the global context stats.
        thread-safe.
        """
        import threading
        if not hasattr(self, "_time_lock"):
             object.__setattr__(self, "_time_lock", threading.Lock())

        with self._time_lock:
            current = self.time_stats.get("total_seconds", 0.0)
            self.time_stats["total_seconds"] = current + seconds

    def summary(self) -> str:
        """Returns a high-level summary of the context."""
        return f"Context(Drug='{self.drug_name}', Target='{self.target_protein}', Papers={len(self.relevant_papers)})"

    def get_agent_context(self) -> str:
        """
        Serializes the context into a rich Markdown string for Specialists.
        Includes drug metadata and paper listing (used by specialist prompts).
        """
        lines = [
            f"# Drug Context: {self.drug_name}",
            f"**Target Class**: {self.target_protein}",
            "",
            "## Key Literature Evidence"
        ]

        if not self.relevant_papers:
            lines.append("No specific literature found.")

        for i, p in enumerate(self.relevant_papers, 1):
            title = p.get('title', 'Unknown Title')
            year = p.get('year')
            if not year or str(year).strip().lower() in ['unknown', 'none', 'nan', '']:
                year = '????'

            lines.append(f"### {i}. {title} ({year})")

            lines.append(f"[Source]({p.get('url', '#')})")
            lines.append("")

        return "\n".join(lines)

    def get_minimal_context(self) -> str:
        """
        Returns minimal context header for PI/Reviewer prompts.
        Papers are not included here since they appear in {{verified_sources}}.
        """
        return f"# Drug Context: {self.drug_name}"
