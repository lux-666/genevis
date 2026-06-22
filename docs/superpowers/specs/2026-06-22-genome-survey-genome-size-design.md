# Genome Survey genome-size handoff

## Goal

Remove the requirement to obtain a genome-size value from the user before the
workflow starts. Genome Survey is the normal producer of the value consumed by
assembly.

## Scope

- Keep the existing `genome_size_prior` field as the single assembly-facing
  interface.
- Its default meaning is "awaiting a later Genome Survey result".
- An explicitly supplied user value remains valid and takes precedence over a
  computed value.
- Do not promote heterozygosity or repeat estimates into required inputs or a
  new TUI contract. They remain Genome Survey result metadata.

## Flow

1. The ask/planner flow may defer `genome_size` only when Genome Survey is
   selected and short DNA reads are present; otherwise it keeps collecting the
   value as an assembly parameter.
2. When clean short DNA reads are available, the existing Genome Survey stage
   runs before assembly.
3. On success, Genome Survey writes its genome-size estimate into the existing
   `genome_size_prior` path. Assembly consumes that field without requiring a
   second representation.
4. When the user supplied an explicit `genome_size_prior`, retain it instead of
   replacing it with the estimate.
5. If Genome Survey is unavailable, fails, or does not yield a usable estimate
   while the value is still awaiting Survey, halt before assembly and expose a
   machine-readable result:

   ```json
   {
     "status": "failed",
     "error_code": "GENOME_SIZE_UNAVAILABLE",
     "genome_size_prior": null
   }
   ```

## Failure semantics

The failure is intentional: do not ask for a late user override or silently
select another assembler. Preserve a human-readable reason and the underlying
survey failure details alongside the stable error code. An explicit user value
may skip Survey when clean short DNA reads are unavailable.

## Tests

- An absent user genome size does not generate an initial clarification.
- A successful survey supplies the genome size seen by assembly.
- An explicit user value remains the assembly input after a successful survey.
- Missing, skipped, failed, or estimate-less Survey produces the stable failed
  result and prevents assembly from starting.
