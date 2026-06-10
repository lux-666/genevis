# Route Selection Skill

Route selection is a data-quality decision. First check which read refs exist,
then platform, then coverage and target level.

Choose `hifi` when PacBio HiFi reads are present and there is no explicit
reason to use another route. Choose `ont` for high-quality ONT R10-style data.
Choose `cyclone` for CycloneSeq or ONT R9-like data. Choose
`insufficient_data` when the route would be a guess because read refs,
platform, or minimum evidence are missing.

Always include uncertainty notes when platform or coverage is unclear.
