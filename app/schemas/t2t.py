"""Shared T2T threshold values for proposals and execution."""

from pydantic import BaseModel, ConfigDict, Field


class T2TThresholds(BaseModel):
    """Complete threshold domain; baseline defaults belong to the catalog."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    min_motif_copies: int = Field(ge=1)
    terminal_window_bp: int = Field(ge=1)
    min_mapq: int = Field(ge=0, le=255)
    max_target_terminal_distance_bp: int = Field(ge=0)
    min_overlap_bp: int = Field(ge=1)
    min_supporting_reads: int = Field(ge=1)
