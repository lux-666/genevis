#!/usr/bin/env bash
# Quick reference for stage blocking patterns

cat << 'EOF'
╔══════════════════════════════════════════════════════════════════════════════╗
║                    GENE AGENT STAGE BLOCKING REFERENCE                        ║
╚══════════════════════════════════════════════════════════════════════════════╝

┌─────────────────────────────────────────────────────────────────────────────┐
│ NON-BLOCKING STAGES (Auto-execute)                                          │
├─────────────────────────────────────────────────────────────────────────────┤
│ • read_qc          : seqkit stats → length filter → FOFN → commit           │
│ • genome_survey    : jellyfish → genomescope2 → commit                      │
│ • assembly_dedup   : deduplication logic → commit                           │
│ • repeat_annotation: RepeatMasker/Modeler → commit                          │
│ • function_annotation: DIAMOND/InterProScan → commit                        │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│ CONDITIONAL BLOCKING STAGES (Block on conditions)                           │
├──────────────┬──────────────────────────────────────────────────────────────┤
│ assembly     │ Gate 1: Route Selection                                      │
│              │   Trigger: LLM confidence < 0.7 OR manual policy             │
│              │   Options: select_ont, select_hifi, select_cyclone           │
│              │                                                               │
│              │ Gate 2: Candidate Selection                                  │
│              │   Trigger: Multiple candidates, no clear winner              │
│              │   Options: Select from candidate comparison                  │
├──────────────┼──────────────────────────────────────────────────────────────┤
│ hic_scaffolding Gate: External Execution (Soft Block)                       │
│              │   Trigger: execution_mode != "local"                         │
│              │   Options: Submit HPC job, Skip stage                        │
├──────────────┼──────────────────────────────────────────────────────────────┤
│ t2t          │ Gate: Review Proposed Patches                                │
│              │   Trigger: Significant structural changes (telomere/gaps)    │
│              │   Options: Accept patches, Reject patches                    │
├──────────────┼──────────────────────────────────────────────────────────────┤
│ gene_annotation Gate: Candidate Bundle Approval                             │
│              │   Trigger: Multiple prediction tools disagree                │
│              │   Options: Select bundle, Merge strategies                   │
└──────────────┴──────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│ MANDATORY BLOCKING STAGES (Always require human review)                     │
├──────────────┬──────────────────────────────────────────────────────────────┤
│ assembly_review Gate: Quality Metrics Review                                │
│              │   QUAST metrics + BUSCO completeness                         │
│              │   Options: Select candidate, Reject all, Re-assemble         │
└──────────────┴──────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│ BLOCKING MECHANISM                                                           │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  1. Node detects blocking condition                                         │
│     ↓                                                                        │
│  2. Set state["stage_outputs"][stage]["status"] = "blocked_human"           │
│     ↓                                                                        │
│  3. Set state["active_interrupt"] = {                                       │
│         "stage": "assembly",                                                │
│         "gate_id": "route_selection",                                       │
│         "decisions_allowed": ["select_ont", "select_hifi"]                  │
│     }                                                                        │
│     ↓                                                                        │
│  4. Set state["supervision_state"] = "blocked_human"                        │
│     ↓                                                                        │
│  5. Routing function: if status == "blocked_human" → return "block"         │
│     ↓                                                                        │
│  6. Graph edge: graph.add_edge("block", END)                                │
│     ↓                                                                        │
│  7. Pipeline exits cleanly, returns to TUI                                  │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│ RESUME FLOW                                                                  │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  TUI detects: supervision_state == "blocked_human"                          │
│     ↓                                                                        │
│  TUI reads: active_interrupt.gate_id                                        │
│     ↓                                                                        │
│  TUI renders: Gate-specific UI with decisions_allowed                       │
│     ↓                                                                        │
│  User selects: decision = "select_ont"                                      │
│     ↓                                                                        │
│  TUI calls: resume_review_state(session_id, stage, gate_id, decision)      │
│     ↓                                                                        │
│  Gate handler applies decision:                                             │
│    - Updates stage_outputs with selected route/candidate                    │
│    - Clears active_interrupt                                                │
│    - Sets supervision_state = "running_stage"                               │
│     ↓                                                                        │
│  Checkpoint updated, pipeline resumes from last node                        │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│ TOOL EXECUTION PATTERN (All bioinformatics tools)                           │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  freeze → run → collect                                                     │
│    │      │      │                                                           │
│    │      │      └─→ Parse output, write {method}_summary.json              │
│    │      │                                                                  │
│    │      └─→ Execute shell script: run_{method}.sh                         │
│    │          (Can be local or HPC sbatch)                                  │
│    │                                                                          │
│    └─→ Generate execution script from template                              │
│        Write to: work/{stage}/{session_id}/{method}/run_{method}.sh         │
│                                                                              │
│  Artifacts published to: artifacts/{stage}/{session_id}/{outputs}           │
│  Ledger records: method_attempt_started, method_attempt_succeeded           │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│ STAGE TRANSITION CHAIN                                                       │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  inventory → read_qc → genome_survey → assembly → assembly_dedup →         │
│  assembly_review → hic_scaffolding → t2t → repeat_annotation →             │
│  gene_annotation → function_annotation → done                               │
│                                                                              │
│  After each stage completes:                                                │
│    - current_stage advances to next in STAGE_ORDER                          │
│    - stage_outputs[stage] contains all outputs                              │
│    - Asset registry updated with new assets                                 │
│    - Parent graph's route_controller dispatches to next stage               │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│ KEY STATE FIELDS                                                             │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  supervision_state:                                                         │
│    - "idle"            : Parent graph routing                               │
│    - "running_stage"   : Stage executing                                    │
│    - "blocked_human"   : Waiting for human decision                         │
│    - "failed_terminal" : Unrecoverable failure                              │
│    - "completed"       : All stages finished                                │
│                                                                              │
│  current_stage:        : Name of stage currently executing                  │
│  active_interrupt:     : Gate information when blocked                      │
│  stage_outputs:        : Dict of all stage outputs                          │
│  asset_registry_ref:   : Path to asset registry JSON                        │
│  requested_stages:     : List of stages to run (from run-config)            │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│ PHASE 9 TEST COVERAGE                                                        │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ✓ Stage order validation (via ledger stage_entered events)                │
│  ✓ Blocking detection (supervision_state, active_interrupt)                │
│  ✓ Clean exits (no crashes when blocking)                                  │
│  ✓ Tool execution (script generation and execution)                         │
│  ✓ Asset propagation (registry updates across stages)                      │
│  ✓ Checkpoint persistence (resume capability)                              │
│  ✓ TUI contract validation (gate structure completeness)                   │
│                                                                              │
│  Run with: cd tests/hpc && ./test_09_e2e_pipeline.sh                       │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘

EOF
