# Input Profiler Skill

Build a factual read profile before recommending routes. Prefer explicit
run-config refs, then stage outputs, then asset registry entries. Do not
invent coverage, platform, or genome-size values when they are absent.

When both HiFi and ONT/Cyclone reads exist, keep both explicit refs. The
primary long-read ref may point to HiFi for data-quality reasons, but ONT and
Cyclone routes must still use ONT-compatible refs.
